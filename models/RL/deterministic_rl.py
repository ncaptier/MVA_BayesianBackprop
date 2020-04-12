import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from models.RL.rl_utils import RLReg, AgentDN


class DeterministicRLNet(nn.Module):
    """ Defines a neural network with two hidden layers of size hidden_size. A
        relu activation is applied after the hidden layer.
    """

    def __init__(self, hidden_size, dim_context, dim_action_space):
        super().__init__()
        self.fc1 = nn.Linear(dim_context, hidden_size)
        # self.fc2 = nn.Linear(hidden_size, hidden_size)
        self.fc3 = nn.Linear(hidden_size, dim_action_space)
        self.layers = [self.fc1, self.fc3]

    def forward(self, x):
        out = F.relu(self.fc1(x))
        # out = F.relu(self.fc2(out))
        return self.fc3(out)

    def weights_dist(self):
        """ Return flatten numpy array containing all the weights of the net """
        return np.hstack(list(map(lambda layer: layer.weight.data.numpy().flatten(), self.layers)))


class DeterministicRLReg(RLReg):
    """ Class for training an AgentDN """

    def __init__(self, X_train, y_train, agent, criterion=torch.nn.MSELoss(), buffer_size=4096, minibatch_size=64,
                 burn_in=500):
        super(DeterministicRLReg, self).__init__(X_train, y_train, agent, buffer_size, minibatch_size, burn_in)
        self.criterion = criterion

    def get_loss_(self, context_inds, actions, rewards):
        rewards_preds = self.agent.evaluate(self.X_train[context_inds])
        reward_preds = rewards_preds[np.arange(self.minibatch_size), actions]
        loss = self.criterion(reward_preds, rewards)
        return loss