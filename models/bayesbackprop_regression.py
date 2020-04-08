import time

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from tensorboardX import SummaryWriter
from torch.utils import data


class VarPosterior(object):

    def __init__(self, mu, rho):
        self.mu = mu
        self.rho = rho
        self.gaussian = torch.distributions.Normal(0, 1)

    @property
    def sigma(self):
        assert torch.max(torch.log1p(torch.exp(self.rho))) < np.inf, self.rho.max()
        return torch.log1p(torch.exp(self.rho))

    def sample(self):
        epsilon = self.gaussian.sample(self.rho.size())
        return self.mu + self.sigma * epsilon

    def log_prob(self, x):
        return torch.sum(-0.5 * torch.log(2 * np.pi * self.sigma ** 2) - 0.5 * (x - self.mu) ** 2 / self.sigma ** 2)


class Prior(object):

    def __init__(self, sigma1, sigma2, pi):
        self.sigma1 = sigma1
        self.sigma2 = sigma2
        self.pi = pi
        self.gaussian1 = torch.distributions.Normal(0, sigma1)
        self.gaussian2 = torch.distributions.Normal(0, sigma2)

    def sample(self):
        x = np.random.binomial(1, self.pi)
        return x * self.gaussian1.sample(torch.Size([1])) + (1 - x) * self.gaussian2.sample(torch.Size([1]))

    def log_prob(self, x):
        filter = self.gaussian1.log_prob(x) > self.gaussian2.log_prob(x)
        result = torch.sum(np.log(self.pi) + self.gaussian1.log_prob(x)[filter] + torch.log1p(
            (1 - self.pi) / self.pi * torch.exp(
                self.gaussian2.log_prob(x)[filter] - self.gaussian1.log_prob(x)[filter])))
        assert np.inf > result > -np.inf, (result, filter, x[filter])
        result += torch.sum(np.log(1 - self.pi) + self.gaussian2.log_prob(x)[~filter] + torch.log1p(
            self.pi / (1 - self.pi) * torch.exp(
                self.gaussian1.log_prob(x)[~filter] - self.gaussian2.log_prob(x)[~filter])))
        assert np.inf > result > -np.inf, (result, filter, x[~filter])
        return result
        # return torch.sum(
        #     torch.log(self.pi * self.gaussian1.log_prob(x).exp() + (1 - self.pi) * self.gaussian2.log_prob(x).exp()))


class BayesianLinear(nn.Module):

    def __init__(self, dim_input, dim_output, prior_parameters):
        super(BayesianLinear, self).__init__()

        self.dim_input = dim_input
        self.dim_output = dim_output
        self.prior_parameters = prior_parameters

        self.w_mu = nn.Parameter(torch.Tensor(dim_output, dim_input).normal_(0, 1))
        self.w_rho = nn.Parameter(torch.Tensor(dim_output, dim_input).normal_(0, 1))
        self.w = VarPosterior(self.w_mu, self.w_rho)
        self.w_prior = Prior(prior_parameters['sigma1'], prior_parameters['sigma2'], prior_parameters['pi'])

        self.b_mu = nn.Parameter(torch.Tensor(dim_output).normal_(0, 1))
        self.b_rho = nn.Parameter(torch.Tensor(dim_output).normal_(0, 1))
        self.b = VarPosterior(self.b_mu, self.b_rho)
        self.b_prior = Prior(prior_parameters['sigma1'], prior_parameters['sigma2'], prior_parameters['pi'])

        self.log_prior = 0
        self.log_variational_posterior = 0

    def forward(self, x):
        w = self.w.sample()
        b = self.b.sample()

        self.log_prior = self.w_prior.log_prob(w) + self.b_prior.log_prob(b)
        self.log_variational_posterior = self.w.log_prob(w) + self.b.log_prob(b)

        return F.linear(x, w, b)

    def get_weights_mu(self):
        """ Auxiliary function used to get the weight distribution of a net """
        return np.hstack([self.w_mu.detach().numpy().flatten(), self.b_mu.detach().numpy().flatten()])


class BayesBackpropNet(nn.Module):

    def __init__(self, hidden_size, dim_input, dim_output, prior_parameters, sigma):
        super(BayesBackpropNet, self).__init__()
        self.fc1 = BayesianLinear(dim_input=dim_input, dim_output=hidden_size
                                  , prior_parameters=prior_parameters)
        self.fc2 = BayesianLinear(dim_input=hidden_size, dim_output=hidden_size
                                  , prior_parameters=prior_parameters)
        self.fc3 = BayesianLinear(dim_input=hidden_size, dim_output=dim_output
                                  , prior_parameters=prior_parameters)

        self.sigma = sigma  # noise associated with the data y = f(x; w) + N(0, self.sigma)

    def forward(self, x):
        out = F.relu(self.fc1(x))
        out = F.relu((self.fc2(out)))
        out = self.fc3(out)
        return out

    def log_prior(self):
        """ Compute log(p(w)) """
        return self.fc1.log_prior + self.fc2.log_prior + self.fc3.log_prior

    def log_variational_posterior(self):
        """ Compute log(q(w|D)) """
        return self.fc1.log_variational_posterior + \
               self.fc2.log_variational_posterior + \
               self.fc3.log_variational_posterior

    def log_likelihood(self, y, output):
        """ Compute log(p(D|w))
        
            Rmk: y_i = f(x_i ; w) + epsilon (epsilon ~ N(0 , self.sigma)) 
                 So we have p(y_i | x_i , w) = N(f(x_i ; w) , self.sigma)
        """
        return torch.sum(-0.5 * np.log(2 * np.pi * self.sigma ** 2) - 0.5 * (y - output) ** 2 / self.sigma ** 2)

    def sample_elbo(self, x, y, MC_samples, weight):
        """ For a batch x compute weight * E(log(q(w|D)) - log(p(w))) - E(log(p(D |w)))
            The expected values are computed with a MC scheme (at each step w is sampled
            from q(w | D))
        """
        elbo = 0
        log_var_posteriors = 0
        log_priors = 0
        log_likelihoods = 0
        for s in range(MC_samples):
            out = self.forward(x).squeeze()
            log_var_posterior = self.log_variational_posterior() * weight
            assert log_var_posterior < np.inf, log_var_posterior
            log_var_posteriors += log_var_posterior
            log_prior = self.log_prior() * weight
            assert log_prior < np.inf, log_prior
            log_priors += log_prior
            log_likelihood = self.log_likelihood(y, out)
            assert -log_likelihoods < np.inf, log_likelihoods
            log_likelihoods += log_likelihood
            elbo += log_var_posterior - log_prior - log_likelihood  # * weight
        return elbo / MC_samples, log_var_posteriors / MC_samples, log_priors / MC_samples, log_likelihoods / MC_samples

    def weights_dist(self):
        """ Return flatten numpy array containing all the weights of the net """
        return np.hstack([self.fc1.get_weights_mu(),
                          self.fc2.get_weights_mu(),
                          self.fc3.get_weights_mu()])


class BayesBackpropReg(object):

    def __init__(self, X_train, y_train, X_test, net, batch_size):
        self.net = net
        self.batch_size = batch_size
        self.X_train = X_train
        self.y_train = y_train
        self.X_test = X_test
        self.pred, self.pred_mean, self.pred_std = None, None, None
        self.batches = self.create_batches()
        self.nb_batches = len(self.batches)
        self.writer = SummaryWriter()  # to get learning curves: tensorboard --logdir=runs (in console)
        self.step = 0

    def create_batches(self):
        torch_train_dataset = data.TensorDataset(self.X_train, self.y_train)
        return data.DataLoader(torch_train_dataset, batch_size=self.batch_size)

    def train(self, epochs, optimizer, MC_samples, weights='uniform', pi=None):
        self.net.train()
        t = time.time()
        for epoch in range(int(epochs)):
            i = 0
            elbos, log_var_posteriors, log_priors, log_likelihoods = 0, 0, 0, 0
            for local_batch, local_labels in self.batches:
                i += 1
                self.step += 1
                optimizer.zero_grad()
                if weights == 'uniform':
                    weight = 1 / len(self.batches)
                elif weights == 'geometric':
                    weight = 2 ** (self.nb_batches - i) / (2 ** self.nb_batches - 1)
                else:
                    raise ValueError("wrong argument for @weight")
                loss, log_var_posterior, log_prior, log_likelihood = self.net.sample_elbo(local_batch, local_labels,
                                                                                          MC_samples, weight=weight)
                loss.backward()
                optimizer.step()
                elbos += loss
                log_var_posteriors += log_var_posterior
                log_priors += log_prior
                log_likelihoods += log_likelihood
            self.writer.add_scalar('loss/elbo', elbos, epoch)
            self.writer.add_scalar('loss/complexity_cost', log_var_posteriors - log_priors, epoch)
            self.writer.add_scalar('loss/negative log-likelihood', - log_likelihoods, epoch)
            self.writer.add_scalar('execution_time', time.time() - t, epoch)
            if epoch % 50 == 0:
                print(f"{epoch:4d}: {elbos:f}")
        return

    def predict(self, samples):
        self.net.eval()
        self.pred = torch.zeros((self.X_test.shape[0], self.y_train.unsqueeze(dim=1).shape[1], samples))
        for s in range(samples):
            self.pred[:, :, s] = self.net(self.X_test).detach()

        self.pred_mean = torch.mean(self.pred, dim=2).squeeze()
        self.pred_std = torch.std(self.pred, dim=2).squeeze()

        return self.pred_mean, self.pred_std

    def plot_results(self, ax=None):
        if ax is None:
            ax = plt.subplot()

        X_test = self.X_test.squeeze().numpy()
        y_pred = self.pred_mean.squeeze().numpy()
        std_pred = self.pred_std.squeeze().numpy()

        ax.fill_between(X_test, y_pred - std_pred * 3, y_pred + std_pred * 3, color='mistyrose', label='3 std. int.')
        ax.fill_between(X_test, y_pred - std_pred * 2, y_pred + std_pred * 2, color='lightcoral', label='2 std. int.')
        ax.fill_between(X_test, y_pred - std_pred, y_pred + std_pred, color='indianred', label='1 std. int.')

        ax.scatter(self.X_train.numpy(), self.y_train.numpy(), color='red', marker='x', label="training points")
        ax.plot(X_test, y_pred, color='blue', label="prediction")
        return


if __name__ == '__main__':
    N = 100  # number of training data points
    sigma = 0.02
    dataset = {}


    def function(x, epsilon):
        return x + 0.3 * np.sin(2 * np.pi * (x + epsilon)) + 0.3 * np.sin(4 * np.pi * (x + epsilon)) + epsilon


    dataset['X_train'] = np.random.uniform(0, 0.5, N)
    dataset['y_train'] = function(dataset['X_train'], np.random.normal(0, sigma, N))
    X_train_tensor = torch.from_numpy(dataset['X_train'].copy()).float().unsqueeze(dim=1)
    y_train_tensor = torch.from_numpy(dataset['y_train'].copy()).float()
    dataset['X_test'] = np.linspace(-0.25, 1, 1000)
    X_test_tensor = torch.from_numpy(dataset['X_test'].copy()).float().unsqueeze(dim=1)

    prior_parameters = {'sigma1': 1, 'sigma2': np.exp(-6), 'pi': 0.5}
    net = BayesBackpropNet(hidden_size=100, dim_input=1, dim_output=1, prior_parameters=prior_parameters, sigma=0.1)
    reg_model = BayesBackpropReg(X_train_tensor, y_train_tensor, X_test_tensor, net, batch_size=10)

    optimizer = torch.optim.Adam(net.parameters(), lr=0.001, weight_decay=1e-6)
    reg_model.train(1000, optimizer, 2)
