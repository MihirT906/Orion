"""Primitive GANF

This primitive is an pytorch implementation of "Graph-Augmented
Normalizing Flows for Anomaly Detection of Multiple Time Series"
https://arxiv.org/pdf/2202.07857.pdf

This is a modified version of the original code, which can be found
at https://github.com/EnyanDai/GANF/blob/main/models/GANF.py
"""
import logging
import os
import random
from inspect import signature

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from sklearn.metrics import roc_auc_score
from torch.nn.init import xavier_uniform_
from torch.nn.utils import clip_grad_value_
from torch.utils.data import DataLoader, Dataset
from tqdm import tqdm

from orion.primitives.nf import MAF, RealNVP

LOGGER = logging.getLogger(__name__)


class Signal(Dataset):
    def __init__(self, df, label=None, window_size=60, stride_size=10, unit='s'):
        super(Signal, self).__init__()
        if label is None:
            self.has_labels = False
        else:
            self.has_labels = True

        self.df = df
        self.window_size = window_size
        self.stride_size = stride_size
        self.unit = unit
        self.data, self.idx, self.label = self.preprocess(df, label)

    def preprocess(self, df, label):
        start_idx = np.arange(0, len(df) - self.window_size, self.stride_size)
        end_idx = np.arange(self.window_size, len(df), self.stride_size)

        delta_time = df.index[end_idx] - df.index[start_idx]
        idx_mask = delta_time == pd.Timedelta(self.window_size, unit=self.unit)

        if self.has_labels:
            return df.values, start_idx[idx_mask], label[start_idx[idx_mask]]

        return df.values, start_idx[idx_mask], None

    def __len__(self):
        return len(self.idx)

    def __getitem__(self, index):
        #  N X K X L X D
        start = self.idx[index]
        end = start + self.window_size
        data = self.data[start:end].reshape([self.window_size, -1, 1])

        if self.has_labels:
            return torch.FloatTensor(data).transpose(0, 1), self.label[index]

        return torch.FloatTensor(data).transpose(0, 1)


class GNN(nn.Module):
    """
    The GNN module applied in GANF
    """

    def __init__(self, input_size, hidden_size):

        super(GNN, self).__init__()
        self.lin_n = nn.Linear(input_size, hidden_size)
        self.lin_r = nn.Linear(input_size, hidden_size, bias=False)
        self.lin_2 = nn.Linear(hidden_size, hidden_size)

    def forward(self, h, A):
        # A: K X K
        # H: N X K  X L X D

        h_n = self.lin_n(torch.einsum('nkld,kj->njld', h, A))
        h_r = self.lin_r(h[:, :, :-1])
        h_n[:, :, 1:] += h_r
        h = self.lin_2(F.relu(h_n))

        return h


class GANF(nn.Module):
    def __init__(self, n_blocks, input_size, hidden_size, n_hidden,
                 dropout=0.1, model="MAF", batch_norm=True):
        super(GANF, self).__init__()

        self.rnn = nn.LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            batch_first=True,
            dropout=dropout
        )

        self.gcn = GNN(input_size=hidden_size, hidden_size=hidden_size)

        if model == "MAF":
            self.nf = MAF(
                n_blocks,
                input_size,
                hidden_size,
                n_hidden,
                cond_label_size=hidden_size,
                batch_norm=batch_norm,
                activation='tanh')
        else:
            self.nf = RealNVP(
                n_blocks,
                input_size,
                hidden_size,
                n_hidden,
                cond_label_size=hidden_size,
                batch_norm=batch_norm)

    def forward(self, x, A):
        return self.test(x, A).mean()

    def test(self, x, A):
        # x: N X K X L X D
        full_shape = x.shape

        # reshape: N*K, L, D
        x = x.reshape((x.shape[0] * x.shape[1], x.shape[2], x.shape[3]))
        h, _ = self.rnn(x)

        # resahpe: N, K, L, H
        h = h.reshape((full_shape[0], full_shape[1], h.shape[1], h.shape[2]))

        h = self.gcn(h, A)

        # reshappe N*K*L,H
        h = h.reshape((-1, h.shape[3]))
        x = x.reshape((-1, full_shape[3]))

        # *full_shape[1]*full_shape[2]
        log_prob = self.nf.log_prob(x, h).reshape([full_shape[0], -1])
        log_prob = log_prob.mean(dim=1)

        return log_prob

    def locate(self, x, A):
        # x: N X K X L X D
        full_shape = x.shape

        # reshape: N*K, L, D
        x = x.reshape((x.shape[0] * x.shape[1], x.shape[2], x.shape[3]))
        h, _ = self.rnn(x)

        # resahpe: N, K, L, H
        h = h.reshape((full_shape[0], full_shape[1], h.shape[1], h.shape[2]))

        h = self.gcn(h, A)

        # reshappe N*K*L,H
        h = h.reshape((-1, h.shape[3]))
        x = x.reshape((-1, full_shape[3]))

        # *full_shape[1]*full_shape[2]
        log_prob = self.nf.log_prob(x, h).reshape([full_shape[0], full_shape[1], -1])
        log_prob = log_prob.mean(dim=2)

        return log_prob


class GANFModel(object):
    """GANF model for unsupervised time series anomaly detection.

    Args:
        name (str):
            Name of the model.
        seed (int):
            Random seed to fix in training.
        n_blocks (int):
            Number of blocks to stack in a model (MADE in MAF; Coupling+BN in RealNVP).
        n_components (int):
            Number of Gaussian clusters for mixture of gaussians models.
        hidden_size (int):
            Hidden layer size for MADE (and each MADE block in an MAF).
        n_hidden (int):
            Number of hidden layers in each MADE.
        batch_norm (bool):
            Whether to use batch norm or not.
        batch_size (int):
            Number of example per epoch.
        weight_decay (float):
            Weight decay rate for Adam.
        epochs (int):
            Number of iterations to train the model.
        lr (float):
            Learning rate.
        log_interval (int):
            How often to show loss statistics and save samples.
        max_iter (int):
            Maximum number of evaluations to find ``h``.
        verbose (bool):
            Whether to be on verbose mode or not.
        model_path (str):
            Path to load model if any.
    """

    def __init__(self, name='ganf', seed=18, n_blocks=1, n_components=1, hidden_size=32,
                 n_hidden=1, batch_norm=True, batch_size=512, dropout=0.0, weight_decay=5e-4,
                 epochs=1, learning_rate=2e-3, log_interval=5, max_iter=20, verbose=False,
                 cuda=None, model_path=None):
        self.name = name
        self.seed = seed
        self.n_blocks = n_blocks
        self.n_components = n_components
        self.hidden_size = hidden_size
        self.n_hidden = n_hidden
        self.batch_norm = batch_norm
        self.batch_size = batch_size
        self.dropout = dropout
        self.weight_decay = weight_decay
        self.epochs = epochs
        self.learning_rate = learning_rate
        self.log_interval = log_interval
        self.max_iter = max_iter
        self.verbose = verbose
        self.cuda = cuda or torch.cuda.is_available()
        self.model_path = model_path

        # defaults
        self.timestamp_column = None
        self.target_column = None
        self.channels = None
        self.mean = None
        self.std = None
        self.A = None
        self._fitted = False

        # create model
        self.model = GANF(
            n_blocks=self.n_blocks,
            input_size=1,
            hidden_size=self.hidden_size,
            n_hidden=self.n_hidden,
            dropout=self.dropout,
            batch_norm=self.batch_norm
        )

        if model_path is not None:
            LOGGER.info("Loading model from {}".format(model_path))
            self.model.load_state_dict(torch.load(model_path))
            self._fitted = True

    def __repr__(self):
        indent = 4
        attr_list = list(signature(self.__init__).parameters)

        attrs_str = ',\n'.join(
            '{indent}{attr_name}={attr_val!s}'.format(
                indent=' ' * indent,
                attr_name=attr,
                attr_val=getattr(self, attr)
            ) for attr in attr_list
        )

        return '{clsname}(\n{attrs_str}\n)'.format(
            clsname=type(self).__name__, attrs_str=attrs_str)

    def _fit(self, train_loader, valid_loader, optimizer, iteration, rho,
             alpha, save_path, device, loss_best=None):
        loss_best = loss_best or False

        for epoch in tqdm(range(self.epochs)):
            loss_train = []
            self.model.train()
            for x in train_loader:
                x = x.to(device)

                optimizer.zero_grad()
                loss = -self.model(x, self.A)
                h = torch.trace(torch.matrix_exp(self.A * self.A)) - self.channels
                total_loss = loss + 0.5 * rho * h * h + alpha * h

                total_loss.backward()
                clip_grad_value_(self.model.parameters(), 1)
                optimizer.step()
                loss_train.append(loss.item())
                self.A.data.copy_(torch.clamp(self.A.data, min=0, max=1))

            # evaluate
            loss_val = 0
            if valid_loader is not None:
                self.model.eval()
                loss_val = []
                with torch.no_grad():
                    for x, *y in valid_loader:
                        x = x.to(device)
                        loss = -self.model.test(x, self.A.data).cpu().numpy()
                        loss_val.append(loss)

                loss_val = np.concatenate(loss_val)
                loss_val = np.nan_to_num(loss_val)

                if self.verbose and hasattr(valid_loader.dataset, 'label'):
                    roc_val = roc_auc_score(
                        np.asarray(valid_loader.dataset.label.values, dtype=int), loss_val)

                    print('[{}] Epoch: {}/{}, valid ROC AUC: {}'.format(
                        iteration, epoch, self.epochs, roc_val))

            if self.verbose:
                # will print 0 for valid if not initiated
                print('[{}] Epoch: {}/{}, train -log_prob: {:.2f}, valid -log_prob: {:.2f}'.format(
                    iteration, epoch, self.epochs, np.mean(loss_train), np.mean(loss_val)))

        if self.verbose:
            print('rho: {}, alpha {}, h {}'.format(rho, alpha, h.item()))

        if loss_best and np.mean(loss_val) < loss_best:
            loss_best = np.mean(loss_val)
            torch.save(self.A.data, os.path.join(save_path, "graph_best.pt"))
            torch.save(self.model.state_dict(), os.path.join(
                save_path, "{}_best.pt".format(self.name)))

        if self.log_interval and epoch % self.log_interval == 0:
            torch.save(self.A.data, os.path.join(save_path, "graph_{}.pt".format(epoch)))
            torch.save(self.model.state_dict(), os.path.join(
                save_path, "{}_{}.pt".format(self.name, epoch)))

        return h

    def fit(self, data, timestamp_column='timestamp', target_column='label', valid_split=0.2,
            output_dir='./checkpoint/model'):
        """Train GANF model.

        Args:
            data (pands.DataFrame):
                A dataframe with ``timestamp`` and feature columns used for training.
            timestamp_column (str):
                Name of the ``timestamp`` column.
            target_column (str):
                Name of the ``label`` column.
            valid_split (float):
                A float to split data dataframe to validation set. Data needs to contain a label
                column. Use ``target_column`` to change the target column name.
            output_dir (str):
                Path to folder where to save the model.
        """
        self.timestamp_column = timestamp_column
        self.target_column = target_column

        # ------------------------------------------------------------------------------
        # Prepare data
        # ------------------------------------------------------------------------------
        data[timestamp_column] = pd.to_datetime(data[timestamp_column])
        data = data.set_index(timestamp_column)

        labels = None
        if target_column in data.columns:
            target = data.pop(target_column)
            values_to_categories = dict(enumerate(pd.unique(target)))
            categories_to_values = {
                category: value
                for value, category in values_to_categories.items()
            }
            labels = pd.Series(target).map(categories_to_values)
            labels.index = target.index

        # normalize data
        self.mean = data.mean(axis=0)
        self.std = data.std(axis=0)

        features = (data - self.mean) / self.std
        features = features.dropna(axis=1)
        self.channels = len(features.columns)

        train = features
        valid_loader = None
        # split data
        if valid_split > 0:
            valid_size = int(len(data) * valid_split)
            train = features.iloc[: -valid_size]
            valid = features.iloc[-valid_size:]

            if labels is not None:
                labels = labels.iloc[-valid_size:]

            valid_loader = DataLoader(
                Signal(valid, labels), batch_size=self.batch_size, shuffle=False)

        train_loader = DataLoader(Signal(train), batch_size=self.batch_size, shuffle=True)

        # ------------------------------------------------------------------------------
        # Seeding
        # ------------------------------------------------------------------------------
        device = torch.device("cuda" if self.cuda else "cpu")

        # seed
        random.seed(self.seed)
        np.random.seed(self.seed)
        torch.manual_seed(self.seed)
        if self.cuda:
            torch.cuda.manual_seed(self.seed)

        self.model.to(device)

        # ------------------------------------------------------------------------------
        # Set model hyperparameters
        # ------------------------------------------------------------------------------
        # TODO: make hyperparameters function arguments
        rho = 1.0
        alpha = 0.0
        h_A_old = np.inf
        rho_max = 1e16
        h_tol = 1e-4

        init = torch.zeros([self.channels, self.channels])
        init = xavier_uniform_(init).abs()
        init = init.fill_diagonal_(0.0)
        # self.A = init.clone().detach().to(device).requires_grad_(True) # .to(device)
        self.A = torch.tensor(init, requires_grad=True, device=device)
        print(self.A.is_leaf)
        # ------------------------------------------------------------------------------
        # Saving directory
        # ------------------------------------------------------------------------------
        save_path = os.path.join(output_dir, self.name)
        if not os.path.exists(save_path):
            os.makedirs(save_path)

        # ------------------------------------------------------------------------------
        # Train
        # ------------------------------------------------------------------------------
        loss_best = np.inf
        for iteration in range(self.max_iter):
            while rho < rho_max:
                optimizer = torch.optim.Adam([
                    {'params': self.model.parameters(), 'weight_decay': self.weight_decay},
                    {'params': [self.A]}], lr=self.learning_rate, weight_decay=0.0)

                h = self._fit(
                    train_loader=train_loader,
                    valid_loader=valid_loader,
                    optimizer=optimizer,
                    iteration=iteration,
                    rho=rho,
                    alpha=alpha,
                    save_path=save_path,
                    device=device
                )

                del optimizer
                torch.cuda.empty_cache()

                if h.item() > 0.5 * h_A_old:
                    rho *= 10
                else:
                    break

            h_A_old = h.item()
            alpha += rho * h.item()

            if h_A_old <= h_tol or rho >= rho_max:
                break

        optimizer = torch.optim.Adam([
            {'params': self.model.parameters(), 'weight_decay': self.weight_decay},
            {'params': [self.A]}], lr=self.learning_rate, weight_decay=0.0)

        self._fit(
            train_loader=train_loader,
            valid_loader=valid_loader,
            optimizer=optimizer,
            iteration='final',
            rho=rho,
            alpha=alpha,
            save_path=save_path,
            device=device,
            loss_best=loss_best
        )

    def predict(self, data, return_loss=False):
        """Predict values using the initialized object.

        Args:
            data (ndarray):
                N-dimensional array containing the input sequences for the model.

        Returns:
            (list, float):
                * Predicted values for each input sequence.
                * Test loss value
        """
        data[self.timestamp_column] = pd.to_datetime(data[self.timestamp_column])
        data = data.set_index(self.timestamp_column)

        if self.target_column in data.columns:
            data = data.drop(self.target_column, axis=1)

        test = (data - self.mean) / self.std
        test = test.dropna(axis=1)

        test_loader = DataLoader(Signal(test), batch_size=self.batch_size, shuffle=False)

        device = torch.device("cuda" if self.cuda else "cpu")

        self.model.eval()
        loss_test = []
        with torch.no_grad():
            for x in test_loader:
                x = x.to(device)
                loss = -self.model.test(x, self.A.data).cpu().numpy()
                loss_test.append(loss)

        print(len(loss_test))

        loss_test = np.concatenate(loss_test)
        loss_test = np.nan_to_num(loss_test)

        if return_loss:
            return np.mean(loss_test)