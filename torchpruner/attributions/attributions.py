import numpy as np
import torch
import torch.nn as nn
from torch.nn.modules.conv import _ConvNd
from torch.nn.modules.activation import ReLU, ReLU6, RReLU, LeakyReLU, Sigmoid, Softplus, Tanh
from ..utils import find_best_module_for_attributions
import logging

from abc import ABC, abstractmethod

SUPPORTED_OUT_PRUNING_MODULES = [nn.Linear, _ConvNd]
ACTIVATIONS = [ReLU, ReLU6, RReLU, LeakyReLU, Sigmoid, Softplus, Tanh]


class _AttributionMetric(ABC):
    """ 
    Altered this class to accept a PL object instead of PyTorch model as the first arg.
    """
    def __init__(self, embedder, data_generator, criterion, device, reduction="mean"):
        assert reduction in ["mean", "none", "sum"] or callable(reduction), \
            'Reduction must be a string in ["mean", "none", "sum"] or a function'
        self.embedder = embedder
        self.model = embedder.model
        self.data_gen = data_generator
        self.criterion = criterion
        self.device = device
        self.reduction = reduction
        self.deterministic = False
        self.benchmark = False

    @abstractmethod
    def run(self, module, **kwargs):
        assert any(
            [isinstance(module, t) for t in SUPPORTED_OUT_PRUNING_MODULES]
        ), f"Attributions can be computed only for the following modules {SUPPORTED_OUT_PRUNING_MODULES}"
        return self.find_evaluation_module(module, **kwargs)

    def find_evaluation_module(self, module, find_best_evaluation_module=False):
        if find_best_evaluation_module is True:
            return find_best_module_for_attributions(self.model, module)
        else:
            return module

    def run_all_forward_MMNE(self):
        """
        Changes I made:
            -account for the webloader data loader returning a list of three objects
            -account for the loss function requiring more than the parameters originally provided
            -provide a way to subsample from the dataset
        """
        self.set_deterministic
        cumulative_loss = None
        with torch.no_grad():
            for i in range(self.epoch_len):
                try:
                    (x, x_ideal, y) = next(self.iterator)
                except:
                    self.iterator = iter(self.data_gen)
                    (x, x_ideal, y) = next(self.iterator)
                x, x_ideal, y = x.to(self.device), x_ideal.to(self.device), y.to(self.device)
                loss = self.criterion(self.model(x), x_ideal, y, self.device, self.embedder)
                if cumulative_loss is None:
                    cumulative_loss = loss.reshape(1)
                else:
                    cumulative_loss = torch.cat((cumulative_loss, loss.reshape(1)), 0)
            self.restore_deterministic
            return cumulative_loss

    def run_all_forward(self):
        """
        Run forward pass on all data in `data_gen`, returning loss for each example
        :return: Tensor

        Changes in run_all_forward_MMNE:
            -account for the webloader data loader returning a list of three objects
            -account for the loss function requiring more than the parameters originally provided
        """
        if str(type(self.data_gen)) == "<class 'webdataset.compat.WebLoader'>":
            return self.run_all_forward_MMNE()
        self.set_deterministic()
        cumulative_loss = None
        with torch.no_grad():
            for idx, (x, y) in enumerate(self.data_gen):
                x, y = x.to(self.device), y.to(self.device)
                loss = self.criterion(self.model(x), y, reduction="none")
                if cumulative_loss is None:
                    cumulative_loss = loss
                else:
                    cumulative_loss = torch.cat((cumulative_loss, loss), 0)
            self.restore_deterministic()
            return cumulative_loss

    def run_all_forward_and_backward(self):
        """
        Run forward and backward passes on all data in `data_gen`
        :return: None
        """
        self.set_deterministic()
        for idx, (x, y) in enumerate(self.data_gen):
            x, y = x.to(self.device), y.to(self.device)
            loss = self.criterion(self.model(x), y)
            loss.backward()
        self.restore_deterministic()

    def run_forward_partial(
        self, x=None, y_true=None, to_module=None, from_module=None
    ):
        """
        Run the forward pass on a given data `x`. If target is provided, also computes and
        returns loss. This function assumes the model is equipped with `forward_partial`
        method to run only part of the computational graph.
        :param x:
        :param y_true:
        :param to_module:
        :param from_module:
        :return:
        """
        self.set_deterministic()
        loss = None
        y = self.model.forward_partial(x, to_module=to_module, from_module=from_module,)
        if y_true is not None and to_module is None:
            loss = self.criterion(y, y_true, reduction="none")
        self.restore_deterministic()
        return y, loss

    def aggregate_over_samples(self, attributions):
        """
        Aggregate the attribution computed on each input example according to some reduction.
        While most often the mean is used, there are cases where a different aggregation might
        be preferred.
        :param attributions:
        :return:
        """
        if self.reduction == "mean":
            return np.mean(attributions, 0)
        elif self.reduction == "sum":
            return np.sum(attributions, 0)
        elif self.reduction == "none":
            return attributions
        else:  # a function
            return self.reduction(attributions)

    def set_deterministic(self):
        self.deterministic = torch.backends.cudnn.deterministic
        self.benchmark = torch.backends.cudnn.benchmark
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

    def restore_deterministic(self):
        torch.backends.cudnn.deterministic = self.deterministic
        torch.backends.cudnn.benchmark = self.benchmark


