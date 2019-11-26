import os
import json
import time
import math
import argparse

import numpy as np
import pandas as pd
import networkx as nx
from tqdm import tqdm
from scipy import sparse
import matplotlib.pyplot as plt

import torch
import torch.optim as optim
from torch.utils.data import DataLoader, Dataset, BatchSampler, RandomSampler
import torch.multiprocessing

from model import DeepSNF
from utils.preprocessor import Preprocessor
from data.processdata import read_nets, read_sparse

from torch_geometric.utils import to_undirected, add_self_loops, subgraph, to_dense_adj
from torch_geometric.data import Data, NeighborSampler

import cProfile
from pytorch_memlab import profile


def main(config, out_name=None):
    cuda = torch.cuda.is_available()
    print('Cuda available?', cuda)

    # Check if the config is already loaded.
    if isinstance(config, dict):
        assert out_name is not None
    else:
        out_name = config.replace('.json', '')
        config = json.load(open('config/' + config, 'r'))

    if 'mixed_precision_training' in config:
        use_amp = config['mixed_precision_training']

        if use_amp:

            try:
                from apex import amp
            except ModuleNotFoundError:
                print('apex cannot be found.')

            try:
                opt_level = config['opt_level']
            except ValueError:
                print('`opt_level` must be provided if using mixed precision training.')

            if opt_level != 'O1':
                raise ValueError('Currently, only O1 is supported for pytorch geometric.')
    else:
        use_amp = False

    in_path = config['in_path']
    if in_path[-1] == '*':
        in_path = in_path[:-1]
        names = os.listdir(in_path)
    else:
        names = config['names']
    epochs = config['epochs']
    batch_size = config['batch_size']
    learning_rate = config['learning_rate']
    gat_shapes = config['gat_shapes']
    embedding_size = config['embedding_size']
    save_model = config['save_model']

    if 'sample_while_training' in config:
        sample_while_training = config['sample_while_training']
    else:
        sample_while_training = False

    if sample_while_training:
        assert('sample_rate' in config)
        sample_rate = config['sample_rate']
        assert(sample_rate <= len(names))
    else:
        sample_rate = None

    if 'embedding_out_path' in config:
        out_path = f'{config["embedding_out_path"]}{out_name}_emb.csv'
    else:
        out_path = 'data/predictions/{}_emb.csv'.format(out_name)
    
    model_path = 'models/{}_model.pt'.format(out_name)
    
    if 'plot_loss' in config:
        plot_loss = config['plot_loss']
    else:
        plot_loss = True
    plot_path = 'plots/{}_plot.png'.format(out_name)

    if 'load_pretrained_model' in config:
        load_pretrained_model = config['load_pretrained_model']
    else:
        load_pretrained_model = False

    if 'weight_type' in config:
        weight_type = config['weight_type']
    else:
        weight_type = 'equal'

    if 'save_weights' in config:
        save_weights = config['save_weights']
    else:
        save_weights = False
    
    if 'use_SVD' in config:
        use_SVD = config['use_SVD']
    else:
        use_SVD = False
    
    if 'SVD_dim' in config:
        SVD_dim = config['SVD_dim']
    else:
        SVD_dim = 2048

    if 'pretrained_encoder' in config:
        if '/' in config['pretrained_encoder']:
            pretrained_encoder = torch.load(config['pretrained_encoder'])
        else:
            pretrained_encoder = torch.load('models/' + config['pretrained_encoder'])
    else:
        pretrained_encoder = None

    # Preprocess input networks.
    preprocessor = Preprocessor(
        [in_path + name for name in names], 
        out_name,
        weight_type=weight_type,
        save_weights=save_weights,
        use_SVD=use_SVD,
        SVD_dim=SVD_dim
    )
    index, masks, weights, features, adj = preprocessor.process(cuda=cuda)

    # Create pytorch geometric datasets.
    datasets = [Data(
        edge_index=ad.indices().cpu(), 
        edge_attr=ad.values().reshape((-1, 1)).cuda(), 
        num_nodes=len(index)) for ad in adj]

    # Create dataloaders for each dataset.
    loaders = [NeighborSampler(data,
                               size=0.4,
                               num_hops=gat_shapes['n_layers'],
                               batch_size=batch_size,
                               shuffle=False,
                               add_self_loops=True)
               for data in datasets]

    # Create model.
    print(len(index))
    model = DeepSNF(len(index), gat_shapes, embedding_size, len(datasets), dropout=0, use_SVD=use_SVD, SVD_dim=SVD_dim)
    
    def init_weights(m):
        if hasattr(m, 'weight'):
            torch.nn.init.kaiming_uniform_(m.weight, a=0.1)
            # torch.nn.init.kaiming_normal_(m.bias)
            m.bias.data.fill_(0.01)

    model.apply(init_weights)

    if pretrained_encoder is not None:
        print('Loading pretrained encoder...')
        encoder_keys = list(pretrained_encoder.keys())
        for i in range(len(adj)):
            if i == 0: 
                continue
            new_keys = [key.replace('0', str(i)) for key in encoder_keys]
            for new_key, old_key in zip(new_keys, encoder_keys):
                pretrained_encoder[new_key] = pretrained_encoder[old_key]
        model_dict = {k: v for k, v in model.state_dict().items() if k not in pretrained_encoder}
        pretrained_encoder.update(model_dict)
        model.load_state_dict(pretrained_encoder)
    
    if load_pretrained_model:
        print('Loading pretrained model...')
        model.load_state_dict(torch.load(f'models/{out_name}_model.pt'))

    # Push model and to cuda device, if available.
    if cuda:
        model.cuda()

    # optimizer = optim.Adam(model.parametersrequires_grad=True,
    #                         lr=learning_ratrequires_grad=True
    #                         weight_decay=0)requires_grad=True
    # optimizer = optim.SGD(model.parameters(), lr=learning_rate, momentum=0.9)
    # scheduler = optim.lr_scheduler.CyclicLR(optimizer,
    #                                         base_lr=0.5*learning_rate,
    #                                         max_lr=2*learning_rate,
    #                                         step_size_up=10,
    #                                         mode='triangular')
    # scheduler = optim.lr_scheduler.OneCycleLR(
    #     optimizer, 
    #     learning_rate * 10, 
    #     steps_per_epoch=math.ceil(len(index) / batch_size), 
    #     epochs=epochs
    # )

    # optimizer = optim.SGD(model.parameters(), lr=learning_rate, momentum=0.9)
    optimizer = optim.Adam(model.parameters(), lr=learning_rate, weight_decay=0.)
    if use_amp:
        model, optimizer = amp.initialize(model, optimizer, opt_level=opt_level)

    def masked_weighted_mse(output, target, weight, node_ids, mask):
        """Custom loss.
        """

        sub_indices, sub_values = subgraph(node_ids, target.indices(), edge_attr=target.values(), relabel_nodes=True)
        target = torch.sparse.FloatTensor(sub_indices.cuda(), sub_values).coalesce().to_dense()

        loss = weight * torch.mean(mask.reshape((-1, 1)) *
                                   torch.mean((output - target)**2, dim=-1) *
                                   mask)

        return loss

    def train(rand_net_idx=None):
        """Defines training behaviour.
        """

        # Get random integers for batch.
        rand_int = torch.randperm(len(index))
        int_splits = torch.split(rand_int, batch_size)
        # batch_features = None
        batch_features = features

        # Initialize loaders to current batch.
        if sample_while_training:
            # rand_net_idx = np.random.permutation(len(loaders))[:sample_rate]
            rand_loaders = [loaders[i] for i in rand_net_idx]
            batch_loaders = [l(rand_int) for l in rand_loaders]
            if isinstance(features, list):
                batch_features = [features[i] for i in rand_net_idx]

            # Subset `masks` tensor.
            mask_splits = torch.split(masks[:, rand_net_idx][rand_int], batch_size)

        else:
            batch_loaders = [l(rand_int) for l in loaders]
            mask_splits = torch.split(masks[rand_int], batch_size)
            if isinstance(features, list):
                batch_features = features

        # List of losses.
        losses = [0. for _ in range(len(batch_loaders))]

        # Get the data flow for each input, stored in a tuple.
        for batch_masks, node_ids in zip(mask_splits, int_splits):
            # data_flows = [next(batch_loader).to('cuda') for batch_loader in batch_loaders]
            data_flows = [next(batch_loader) for batch_loader in batch_loaders]

            optimizer.zero_grad()
            if sample_while_training:
                training_datasets = [datasets[i] for i in rand_net_idx]
                output, _, _, _ = model(training_datasets, data_flows, batch_features, batch_masks, rand_net_idxs=rand_net_idx)
                curr_losses = [masked_weighted_mse(output, adj[i], weights[i], node_ids, batch_masks[:, j])
                    for j, i in enumerate(rand_net_idx)]
            else:
                training_datasets = datasets
                output, _, _, _ = model(training_datasets, data_flows, batch_features, batch_masks)
                curr_losses = [masked_weighted_mse(output, adj[i], weights[i], node_ids, batch_masks[:, i])
                    for i in range(len(adj))]   

            losses = [l + cl for l, cl in zip(losses, curr_losses)]

            loss_sum = sum(curr_losses)

            if use_amp:
                with amp.scale_loss(loss_sum, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss_sum.backward()

                # print(datasets[0].edge_attr.grad[0, :10])

                # print(x_store[0].grad[0, :10])
                # print(x_store[1].grad[0, :10])
                # print(x_store[0][0, :10])
                # print(x_store[1][0, :10])
                # print(model.gat_layers[0].weight.grad[0, :10])
                # print(model.gat_layers[1].weight.grad[0, :10])
                # print(model.gat_layers[0].weight.grad.shape)
                # print(model.gat_layers[1].weight.grad.shape)
                # print(model.gat_layers[0].weight[0, :10])
                # print(model.gat_layers[1].weight[0, :10])
                # print(model.gat_layers[1].weight)
                # print(model.pre_gat_layers[0].weight.grad[0, :10])
                # print(model.pre_gat_layers[1].weight.grad[0, :10])
                # print(model.emb.weight)
                # print('\n')

            optimizer.step()
        # scheduler.step()

        return output, losses

    def plot_losses(train_loss, val_loss=None):
        n_epochs = len(train_loss)
        x_epochs = np.arange(n_epochs)

        plt.plot(x_epochs, train_loss, label='Train')
        if val_loss:
            plt.plot(x_epochs, val_loss, label='Validation')
            plt.legend()

        plt.xlabel('Epochs')
        plt.ylabel('Loss')

        plt.savefig(plot_path)

    # Track losses per epoch.
    train_loss = []
    val_loss = []

    best_loss = None
    best_state = None

    # Train model.
    for epoch in range(epochs):

        t = time.time()

        # Track average loss across batches.
        epoch_losses = np.zeros(len(adj))

        if sample_while_training:
            rand_net_idxs = np.random.permutation(len(adj))
            idx_split = np.array_split(rand_net_idxs, math.floor(len(adj)/sample_rate))
            for rand_idxs in idx_split:
                _, losses = train(rand_idxs)
                for idx, loss in zip(rand_idxs, losses):
                    epoch_losses[idx] += loss

        else:
            _, losses = train()

            epoch_losses = [ep_loss + b_loss.item() / (len(index) / batch_size)
                            for ep_loss, b_loss in zip(epoch_losses, losses)]

        # Print training progress.
        print('Epoch: {} |'.format(epoch + 1),
              'Loss Total: {:.6f} |'.format(sum(epoch_losses)), end=' ', flush=True)
        if len(adj) < 10:
            for i, loss in enumerate(epoch_losses):
                print('Loss {}: {:.6f} |'.format(i + 1, loss), end=' ', flush=True)
        print('Time: {:.4f}s'.format(time.time() - t), flush=True)

        train_loss.append(sum(epoch_losses))

        # Store best parameter set.
        if not best_loss or sum(epoch_losses) < best_loss:
            best_loss = sum(epoch_losses)
            state = {
                'epoch': epoch + 1,
                'state_dict': model.state_dict(),
                'best_loss': best_loss
            }
            best_state = state
            # torch.save(state, f'checkpoints/{out_name}_model.pt')

    if plot_loss:
        plot_losses(train_loss)

    # Output embedding.
    print('Forward pass...')
    model.load_state_dict(best_state['state_dict'])
    print(f'Loaded best model from epoch {best_state["epoch"]} with loss {best_state["best_loss"]}.')

    if save_model:
        print('Saving model...')
        torch.save(model.state_dict(), model_path)

    model.eval()
    emb_list = []
    pre_cat_lists = [[] for _ in adj]



    # # Code to get gradients for pairwise similarity (dot product) between genes
    # idx_dct = {idx: i for i, idx in enumerate(index)}
    # pair = ['YDR166C', 'YIL068C']
    # pair_idx = [idx_dct[p] for p in pair]
    # loaders = [NeighborSampler(data,
    #                            size=1.0,
    #                            num_hops=gat_shapes['n_layers'],
    #                            batch_size=2,
    #                            shuffle=False,
    #                            add_self_loops=True)
    #            for data in datasets]
    # loaders = [loader(torch.LongTensor(pair_idx)) for loader in loaders]
    # batch_masks = masks[pair_idx, :]
    # data_flows = [next(loader) for loader in loaders]
    # dot, _, _, learned_weights = model(datasets, data_flows, features, batch_masks, evaluate=True)

    # optimizer.zero_grad()
    # print(dot)
    # dot[0, 1].backward()
    # print(torch.norm(model.gat_layers[0].weight.grad))
    # print(torch.norm(model.gat_layers[1].weight.grad))
    # print('\n') 




    # Redefine dataloaders for each dataset for evaluation.
    loaders = [NeighborSampler(data,
                               size=1.0,
                               num_hops=gat_shapes['n_layers'],
                               batch_size=1,
                               shuffle=False,
                               add_self_loops=True)
               for data in datasets]
    loaders = [loader(torch.arange(len(index))) for loader in loaders]

    for batch_masks, idx in tqdm(zip(masks, index), desc='Forward pass'):
        batch_masks = batch_masks.reshape((1, -1))
        data_flows = [next(loader) for loader in loaders]
        dot, emb, _, learned_weights = model(datasets, data_flows, features, batch_masks, evaluate=True)
        emb_list.append(emb.detach().cpu().numpy())

    emb = np.concatenate(emb_list)
    emb = pd.DataFrame(emb, index=index)
    emb.to_csv(out_path)

    # for i, pre_cat_list in enumerate(pre_cat_lists):
    #     layer = np.concatenate(pre_cat_list)
    #     layer = pd.DataFrame(layer, index=index)
    #     layer.to_csv(out_path[:-4] + f'_{i}.csv')

    torch.save(masks.detach().cpu(), out_path[:-4] + '_masks.pt')
    learned_weights = pd.DataFrame(learned_weights.detach().cpu().numpy(), columns=names).T
    print(learned_weights)
    learned_weights.to_csv(out_path[:-4] + '_learned_weights.csv', header=False)

    # Free memory.
    torch.cuda.empty_cache()

    print('Complete!')


if __name__ == '__main__':
    description = '''Trains model and outputs predicted gene embeddings.
    '''
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('-c', '--config', required=True,
                        help='Name of config file.')
    args = parser.parse_args()

    # cProfile.run('main(str(args.config))')
    main(str(args.config))