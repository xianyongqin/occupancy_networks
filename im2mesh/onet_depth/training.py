import os
from tqdm import trange
import torch
from torch.nn import functional as F
from torch import distributions as dist
from im2mesh.common import (
    compute_iou, make_3d_grid
)
from im2mesh.utils import visualize as vis
from im2mesh.training import BaseTrainer
from im2mesh.onet_depth.models import background_setting

from im2mesh.onet.loss_functions import get_occ_loss, occ_loss_postprocess
from im2mesh.common import get_camera_args, get_world_mat, transform_points, transform_points_back
from im2mesh.encoder.pointnet import feature_transform_reguliarzer, PointNetEncoder, PointNetResEncoder

def depth_to_L(pr_depth_map, gt_mask):
    #not inplace function
    pr_depth_map_max = torch.max(pr_depth_map[gt_mask])
    pr_depth_map_min = torch.min(pr_depth_map[gt_mask])
    background_setting(pr_depth_map, gt_mask, pr_depth_map_max)
    pr_depth_map = (pr_depth_map - pr_depth_map_min) / (pr_depth_map_max - pr_depth_map_min)
    return pr_depth_map

class Phase1Trainer(BaseTrainer):
    ''' Phase1Trainer object for the Occupancy Network.

    Args:
        model (nn.Module): Occupancy Network model
        optimizer (optimizer): pytorch optimizer object
        device (device): pytorch device
        input_type (str): input type
        vis_dir (str): visualization directory
        
    '''

    def __init__(self, model, optimizer, device=None, input_type='img', vis_dir=None, pred_minmax=False):
        self.model = model
        self.optimizer = optimizer
        self.device = device
        self.input_type = input_type
        self.vis_dir = vis_dir
        self.pred_minmax = pred_minmax

        if vis_dir is not None and not os.path.exists(vis_dir):
            os.makedirs(vis_dir)

    def train_step(self, data):
        ''' Performs a training step.

        Args:
            data (dict): data dictionary
        '''
        self.model.train()
        self.optimizer.zero_grad()
        loss = self.compute_loss(data)
        loss.backward()
        self.optimizer.step()
        return loss.item()

    def eval_step(self, data):
        ''' Performs an evaluation step.

        Args:
            data (dict): data dictionary
        '''
        self.model.eval()
        device = self.device
        eval_dict = {}

        with torch.no_grad():
            loss = self.compute_loss(data)

        eval_dict['eval_loss'] = loss.item()
        return eval_dict

    def visualize(self, data):
        ''' Performs a visualization step for the data.

        Args:
            data (dict): data dictionary
        '''
        device = self.device
        inputs = data.get('inputs').to(device)
        gt_depth_maps = data.get('inputs.depth')
        gt_masks = data.get('inputs.mask').byte()
        batch_size = gt_depth_maps.size(0)
        
        kwargs = {}
        self.model.eval()
        with torch.no_grad():
            pr_depth_maps = self.model.predict_depth_map(inputs).cpu()
        
        for i in trange(batch_size):
            gt_depth_map = gt_depth_maps[i]
            pr_depth_map = pr_depth_maps[i]
            gt_mask = gt_masks[i]
            
            pr_depth_map = depth_to_L(pr_depth_map, gt_mask)
            gt_depth_map = depth_to_L(gt_depth_map, gt_mask)

            input_img_path = os.path.join(self.vis_dir, '%03d_in.png' % i)
            input_depth_path = os.path.join(self.vis_dir, '%03d_in_depth.png' % i)
            pr_depth_path = os.path.join(self.vis_dir, '%03d_pr_depth.png' % i)
            vis.visualize_data(inputs[i].cpu(), 'img', input_img_path)
            vis.visualize_data(gt_depth_map, 'img', input_depth_path)
            vis.visualize_data(pr_depth_map, 'img', pr_depth_path)

    def compute_loss(self, data):
        ''' Computes the loss.

        Args:
            data (dict): data dictionary
        '''
        device = self.device
        inputs = data.get('inputs').to(device)
        gt_depth_maps = data.get('inputs.depth').to(device)
        gt_mask = data.get('inputs.mask').to(device)
        pr_depth_maps = self.model.predict_depth_maps(inputs)
        n_predicts = pr_depth_maps.size(1)

        mask_pix_count = gt_mask.sum()

        loss = 0
        for i in range(n_predicts):
            # for object
            if mask_pix_count != 0.:
                loss += (F.mse_loss(pr_depth_maps[:,i], gt_depth_maps, reduce=False) * gt_mask).sum() / mask_pix_count
            # for background
            #loss += ( F.relu(5.0 - pr_depth_maps[:,i]) * (1. - gt_mask) ).mean()
            #loss += 0.1 * ( F.sigmoid(pr_depth_maps[:,i]) * (-1.0) * (1. - gt_mask) ).mean()

        if self.pred_minmax:
            gt_min = data.get('inputs.depth_min').to(device)
            gt_max = data.get('inputs.depth_max').to(device)
            predicted_minmax = self.model.fetch_minmax()
            w_minmax = (224 ** 2) / 2
            loss += w_minmax * F.mse_loss(predicted_minmax[:,0], gt_min)
            loss += w_minmax * F.mse_loss(predicted_minmax[:,1], gt_max)

        return loss

class Phase2Trainer(BaseTrainer):
    ''' Phase2Trainer object for the Occupancy Network.

    Args:
        model (nn.Module): Occupancy Network model
        optimizer (optimizer): pytorch optimizer object
        device (device): pytorch device
        input_type (str): input type
        vis_dir (str): visualization directory
        threshold (float): threshold value
        eval_sample (bool): whether to evaluate samples

    '''

    def __init__(self, model, optimizer, device=None, input_type='img_with_depth',
                 vis_dir=None, threshold=0.5, eval_sample=False, loss_type='cross_entropy',
                 surface_loss_weight=1.,
                 loss_tolerance_episolon=0.,
                 sign_lambda=0.,
                 training_detach=True,
                 depth_map_mix=False
                ):
        self.model = model
        self.optimizer = optimizer
        self.device = device
        self.input_type = input_type
        self.vis_dir = vis_dir
        self.threshold = threshold
        self.eval_sample = eval_sample
        self.loss_type = loss_type
        self.surface_loss_weight = surface_loss_weight
        self.loss_tolerance_episolon = loss_tolerance_episolon
        self.sign_lambda = sign_lambda
        self.training_detach = training_detach
        self.depth_map_mix = depth_map_mix

        if vis_dir is not None and not os.path.exists(vis_dir):
            os.makedirs(vis_dir)

        if self.training_detach and self.model.depth_predictor is not None:
            for param in self.model.depth_predictor.parameters():
                param.requires_grad = False

    def train_step(self, data):
        ''' Performs a training step.

        Args:
            data (dict): data dictionary
        '''
        self.model.train()
        self.optimizer.zero_grad()
        loss = self.compute_loss(data)
        loss.backward()
        self.optimizer.step()
        return loss.item()

    def eval_step(self, data):
        ''' Performs an evaluation step.

        Args:
            data (dict): data dictionary
        '''
        self.model.eval()

        device = self.device
        threshold = self.threshold
        eval_dict = {}

        # Compute elbo
        points = data.get('points').to(device)
        occ = data.get('points.occ').to(device)

        inputs = data.get('inputs').to(device)
        #gt_depth_maps = data.get('inputs.depth').to(device)
        gt_mask = data.get('inputs.mask').to(device).byte()

        voxels_occ = data.get('voxels')

        points_iou = data.get('points_iou').to(device)
        occ_iou = data.get('points_iou.occ').to(device)

        kwargs = {}

        with torch.no_grad():
            elbo, rec_error, kl = self.model.compute_elbo(
                points, occ, inputs, gt_mask,  **kwargs)

        eval_dict['loss'] = -elbo.mean().item()
        eval_dict['rec_error'] = rec_error.mean().item()
        eval_dict['kl'] = kl.mean().item()

        # Compute iou
        batch_size = points.size(0)

        with torch.no_grad():
            p_out = self.model(points_iou, inputs, gt_mask,
                               sample=self.eval_sample, **kwargs)

        occ_iou_np = (occ_iou >= 0.5).cpu().numpy()
        occ_iou_hat_np = (p_out.probs >= threshold).cpu().numpy()
        iou = compute_iou(occ_iou_np, occ_iou_hat_np).mean()
        eval_dict['iou'] = iou

        # Estimate voxel iou
        if voxels_occ is not None:
            voxels_occ = voxels_occ.to(device)
            points_voxels = make_3d_grid(
                (-0.5 + 1/64,) * 3, (0.5 - 1/64,) * 3, (32,) * 3)
            points_voxels = points_voxels.expand(
                batch_size, *points_voxels.size())
            points_voxels = points_voxels.to(device)
            with torch.no_grad():
                p_out = self.model(points_voxels, inputs, gt_mask,
                                   sample=self.eval_sample, **kwargs)

            voxels_occ_np = (voxels_occ >= 0.5).cpu().numpy()
            occ_hat_np = (p_out.probs >= threshold).cpu().numpy()
            iou_voxels = compute_iou(voxels_occ_np, occ_hat_np).mean()

            eval_dict['iou_voxels'] = iou_voxels

        return eval_dict

    def visualize(self, data):
        ''' Performs a visualization step for the data.

        Args:
            data (dict): data dictionary
        '''
        device = self.device

        batch_size = data['points'].size(0)
        inputs = data.get('inputs').to(device)
        #gt_depth_maps = data.get('inputs.depth').to(device)
        gt_mask = data.get('inputs.mask').to(device).byte()

        shape = (32, 32, 32)
        p = make_3d_grid([-0.5] * 3, [0.5] * 3, shape).to(device)
        p = p.expand(batch_size, *p.size())

        kwargs = {}
        with torch.no_grad():
            pr_depth_maps = self.model.predict_depth_map(inputs)
            background_setting(pr_depth_maps, gt_mask)
            p_r = self.model.forward_halfway(p, pr_depth_maps, sample=self.eval_sample, **kwargs)

        occ_hat = p_r.probs.view(batch_size, *shape)
        voxels_out = (occ_hat >= self.threshold).cpu().numpy()

        for i in trange(batch_size):
            input_img_path = os.path.join(self.vis_dir, '%03d_in.png' % i)
            vis.visualize_data(
                inputs[i].cpu(), 'img', input_img_path)
            vis.visualize_voxels(
                voxels_out[i], os.path.join(self.vis_dir, '%03d.png' % i))

            depth_map_path = os.path.join(self.vis_dir, '%03d_pr_depth.png' % i)
            depth_map = pr_depth_maps[i].cpu()
            depth_map = depth_to_L(depth_map, gt_mask[i].cpu())
            vis.visualize_data(
                depth_map, 'img', depth_map_path)

    def compute_loss(self, data):
        ''' Computes the loss.

        Args:
            data (dict): data dictionary
        '''
        device = self.device
        p = data.get('points').to(device)
        batch_size = p.size(0)
        occ = data.get('points.occ').to(device)

        inputs = data.get('inputs').to(device)
        gt_mask = data.get('inputs.mask').to(device).byte()

        if self.training_detach:
            with torch.no_grad():
                pr_depth_maps = self.model.predict_depth_map(inputs)
        else:
            pr_depth_maps = self.model.predict_depth_map(inputs)

        background_setting(pr_depth_maps, gt_mask)
        if self.depth_map_mix:
            gt_depth_maps = data.get('inputs.depth').to(device)
            background_setting(gt_depth_maps, gt_mask)
            alpha = torch.rand(batch_size,1,1,1).to(device)
            pr_depth_maps = pr_depth_maps * alpha + gt_depth_maps * (1.0 - alpha)

        kwargs = {}
        c = self.model.encode(pr_depth_maps)
        q_z = self.model.infer_z(p, occ, c, **kwargs)
        z = q_z.rsample()

        # KL-divergence
        kl = dist.kl_divergence(q_z, self.model.p0_z).sum(dim=-1)
        loss = kl.mean()

        # General points
        p_r = self.model.decode(p, z, c, **kwargs)
        logits = p_r.logits
        probs = p_r.probs
        
        # loss
        loss_i = get_occ_loss(logits, occ, self.loss_type)
        # loss strategies
        loss_i = occ_loss_postprocess(loss_i, occ, probs, self.loss_tolerance_episolon, self.sign_lambda, self.threshold, self.surface_loss_weight)

        loss = loss + loss_i.sum(-1).mean()

        return loss

def compose_inputs(data, mode='train', device=None, input_type='depth_pred',
    use_gt_depth_map=False, depth_map_mix=False, with_img=False, depth_pointcloud_transfer=None,
    local=False):
    
    assert mode in ('train', 'val', 'test')
    raw_data = {}
    if input_type == 'depth_pred':
        gt_mask = data.get('inputs.mask').to(device).byte()
        raw_data['gt_mask'] = gt_mask

        batch_size = gt_mask.size(0)
        if use_gt_depth_map:
            gt_depth_maps = data.get('inputs.depth').to(device)
            background_setting(gt_depth_maps, gt_mask)
            encoder_inputs = gt_depth_maps
        else:
            pr_depth_maps = data.get('inputs.depth_pred').to(device)
            background_setting(pr_depth_maps, gt_mask)
            if depth_map_mix and mode == 'train':
                gt_depth_maps = data.get('inputs.depth').to(device)
                background_setting(gt_depth_maps, gt_mask)
                alpha = torch.rand(batch_size,1,1,1).to(device)
                pr_depth_maps = pr_depth_maps * alpha + gt_depth_maps * (1.0 - alpha)
            encoder_inputs = pr_depth_maps

        if with_img:
            img = data.get('inputs').to(device)
            encoder_inputs = {'img': img, 'depth': encoder_inputs}
        
        if local:
            camera_args = get_camera_args(data, 'points.loc', 'points.scale', device=device)
            Rt = camera_args['Rt']
            K = camera_args['K']
            encoder_inputs = {
                None: encoder_inputs,
                'world_mat': Rt,
                'camera_mat': K,
            }
            raw_data['world_mat'] = Rt
            raw_data['camera_mat'] = K

        return encoder_inputs, raw_data
    elif input_type == 'depth_pointcloud':
        encoder_inputs = data.get('inputs').to(device)

        if depth_pointcloud_transfer is not None:
            if depth_pointcloud_transfer in ('world', 'world_scale_model'):
                encoder_inputs = encoder_inputs[:, :, [1,0,2]]
                world_mat = get_world_mat(data, transpose=None, device=device)
                raw_data['world_mat'] = world_mat

                R = world_mat[:, :, :3]
                # R's inverse is R^T
                encoder_inputs = transform_points(encoder_inputs, R.transpose(1, 2))
                # or encoder_inputs = transform_points_back(encoder_inputs, R)

                if depth_pointcloud_transfer == 'world_scale_model':
                    t = world_mat[:, :, 3:]
                    encoder_inputs = encoder_inputs * t[:,2:,:]
            elif depth_pointcloud_transfer in ('view', 'view_scale_model'):
                encoder_inputs = encoder_inputs[:, :, [1,0,2]]

                if depth_pointcloud_transfer == 'view_scale_model':
                    world_mat = get_world_mat(data, transpose=None, device=device)
                    raw_data['world_mat'] = world_mat
                    t = world_mat[:, :, 3:]
                    encoder_inputs = encoder_inputs * t[:,2:,:]
            else:
                raise NotImplementedError

        if local:
            #assert depth_pointcloud_transfer.startswith('world')
            encoder_inputs = {
                None: encoder_inputs
            }

        return encoder_inputs, raw_data
    else:
        raise NotImplementedError
    
class Phase2HalfwayTrainer(BaseTrainer):
    ''' Phase2HalfwayTrainer object for the Occupancy Network.

    Args:
        model (nn.Module): Occupancy Network model
        optimizer (optimizer): pytorch optimizer object
        device (device): pytorch device
        input_type (str): input type
        vis_dir (str): visualization directory
        threshold (float): threshold value
        eval_sample (bool): whether to evaluate samples

    '''

    def __init__(self, model, optimizer, device=None, input_type='depth_pred',
                 vis_dir=None, threshold=0.5, eval_sample=False, loss_type='cross_entropy',
                 surface_loss_weight=1.,
                 loss_tolerance_episolon=0.,
                 sign_lambda=0.,
                 use_gt_depth_map=False,
                 depth_map_mix=False,
                 with_img=False,
                 depth_pointcloud_transfer='view',
                 local=False,
                ):
        self.model = model
        self.optimizer = optimizer
        self.device = device
        self.input_type = input_type
        assert input_type == 'depth_pred' or input_type == 'depth_pointcloud'
        self.vis_dir = vis_dir
        self.threshold = threshold
        self.eval_sample = eval_sample
        self.loss_type = loss_type
        self.surface_loss_weight = surface_loss_weight
        self.loss_tolerance_episolon = loss_tolerance_episolon
        self.sign_lambda = sign_lambda
        self.depth_map_mix = depth_map_mix
        self.use_gt_depth_map = use_gt_depth_map
        self.with_img = with_img
        if self.with_img:
            print('Predict using img&depth')
        self.depth_pointcloud_transfer = depth_pointcloud_transfer

        self.local=local
        if self.local:
            print('Predict using local features') 

        assert depth_pointcloud_transfer in ('world', 'world_scale_model', 'view', 'view_scale_model')

        if vis_dir is not None and not os.path.exists(vis_dir):
            os.makedirs(vis_dir)

    def train_step(self, data):
        ''' Performs a training step.

        Args:
            data (dict): data dictionary
        '''
        self.model.train()
        self.optimizer.zero_grad()
        loss = self.compute_loss(data)
        loss.backward()
        self.optimizer.step()
        return loss.item()

    def eval_step(self, data):
        ''' Performs an evaluation step.

        Args:
            data (dict): data dictionary
        '''
        self.model.eval()

        device = self.device
        threshold = self.threshold
        eval_dict = {}

        # Compute elbo
        points = data.get('points').to(device)
        occ = data.get('points.occ').to(device)
        voxels_occ = data.get('voxels')
        points_iou = data.get('points_iou').to(device)
        occ_iou = data.get('points_iou.occ').to(device)

        kwargs = {}

        encoder_inputs, _ = compose_inputs(data, mode='val', device=self.device, input_type=self.input_type,
                                                use_gt_depth_map=self.use_gt_depth_map, depth_map_mix=self.depth_map_mix, 
                                                with_img=self.with_img, depth_pointcloud_transfer=self.depth_pointcloud_transfer,
                                                local=self.local)

        with torch.no_grad():
            elbo, rec_error, kl = self.model.compute_elbo_halfway(
                points, occ, encoder_inputs, **kwargs)

        eval_dict['loss'] = -elbo.mean().item()
        eval_dict['rec_error'] = rec_error.mean().item()
        eval_dict['kl'] = kl.mean().item()

        # Compute iou
        batch_size = points.size(0)

        with torch.no_grad():
            p_out = self.model.forward_halfway(points_iou, encoder_inputs,
                               sample=self.eval_sample, **kwargs)

        occ_iou_np = (occ_iou >= 0.5).cpu().numpy()
        occ_iou_hat_np = (p_out.probs >= threshold).cpu().numpy()
        iou = compute_iou(occ_iou_np, occ_iou_hat_np).mean()
        eval_dict['iou'] = iou

        # Estimate voxel iou
        if voxels_occ is not None:
            voxels_occ = voxels_occ.to(device)
            points_voxels = make_3d_grid(
                (-0.5 + 1/64,) * 3, (0.5 - 1/64,) * 3, (32,) * 3)
            points_voxels = points_voxels.expand(
                batch_size, *points_voxels.size())
            points_voxels = points_voxels.to(device)
            with torch.no_grad():
                p_out = self.model.forward_halfway(points_voxels, encoder_inputs,
                                   sample=self.eval_sample, **kwargs)

            voxels_occ_np = (voxels_occ >= 0.5).cpu().numpy()
            occ_hat_np = (p_out.probs >= threshold).cpu().numpy()
            iou_voxels = compute_iou(voxels_occ_np, occ_hat_np).mean()

            eval_dict['iou_voxels'] = iou_voxels

        return eval_dict

    def visualize(self, data):
        ''' Performs a visualization step for the data.

        Args:
            data (dict): data dictionary
        '''
        device = self.device

        batch_size = data['points'].size(0)
        
        shape = (32, 32, 32)
        p = make_3d_grid([-0.5] * 3, [0.5] * 3, shape).to(device)
        p = p.expand(batch_size, *p.size())

        encoder_inputs, raw_data = compose_inputs(data, mode='val', device=self.device, input_type=self.input_type,
                                                use_gt_depth_map=self.use_gt_depth_map, depth_map_mix=self.depth_map_mix, 
                                                with_img=self.with_img, depth_pointcloud_transfer=self.depth_pointcloud_transfer,
                                                local=self.local)

        kwargs = {}
        with torch.no_grad():
            p_r = self.model.forward_halfway(p, encoder_inputs, sample=self.eval_sample, **kwargs)

        occ_hat = p_r.probs.view(batch_size, *shape)
        voxels_out = (occ_hat >= self.threshold).cpu().numpy()

        # visualize
        if self.local:
            encoder_inputs = encoder_inputs[None]

        if self.input_type == 'depth_pred':
            gt_mask = raw_data['gt_mask']
            if self.with_img:
                encoder_inputs = encoder_inputs['depth']

            for i in trange(batch_size):
                if self.use_gt_depth_map:
                    input_img_path = os.path.join(self.vis_dir, '%03d_in_gt.png' % i)
                else:
                    input_img_path = os.path.join(self.vis_dir, '%03d_in_pr.png' % i)
                
                depth_map = encoder_inputs[i].cpu()
                depth_map = depth_to_L(depth_map, gt_mask[i].cpu())
                vis.visualize_data(
                    depth_map, 'img', input_img_path)
                vis.visualize_voxels(
                    voxels_out[i], os.path.join(self.vis_dir, '%03d.png' % i))
        elif self.input_type == 'depth_pointcloud':
            for i in trange(batch_size):
                input_pointcloud_file = os.path.join(self.vis_dir, '%03d_depth_pointcloud.png' % i)
                
                pc = encoder_inputs[i].cpu()
                vis.visualize_pointcloud(pc, out_file=input_pointcloud_file, elev=15, azim=180)
                vis.visualize_voxels(voxels_out[i], os.path.join(self.vis_dir, '%03d.png' % i))
   
    def compute_loss(self, data):
        ''' Computes the loss.

        Args:
            data (dict): data dictionary
        '''
        device = self.device
        p = data.get('points').to(device)
        batch_size = p.size(0)
        occ = data.get('points.occ').to(device)

        encoder_inputs,_ = compose_inputs(data, mode='train', device=self.device, input_type=self.input_type,
                                                use_gt_depth_map=self.use_gt_depth_map, depth_map_mix=self.depth_map_mix, 
                                                with_img=self.with_img, depth_pointcloud_transfer=self.depth_pointcloud_transfer,
                                                local=self.local)

        kwargs = {}
        c = self.model.encode(encoder_inputs, only_feature=False, p=p)
        q_z = self.model.infer_z(p, occ, c, **kwargs)
        z = q_z.rsample()

        # KL-divergence
        kl = dist.kl_divergence(q_z, self.model.p0_z).sum(dim=-1)
        loss = kl.mean()

        # additional loss for feature transform
        if isinstance(c, tuple):
            trans_feature = c[-1]
            if self.local:
                c = (c[0], c[1])
            else:
                c = c[0]
        
            if isinstance(self.model.encoder, PointNetEncoder) or isinstance(self.model.encoder, PointNetResEncoder):
                loss = loss + 0.001 * feature_transform_reguliarzer(trans_feature) 

        # General points
        p_r = self.model.decode(p, z, c, **kwargs)
        logits = p_r.logits
        probs = p_r.probs


        # loss
        loss_i = get_occ_loss(logits, occ, self.loss_type)
        # loss strategies
        loss_i = occ_loss_postprocess(loss_i, occ, probs, self.loss_tolerance_episolon, self.sign_lambda, self.threshold, self.surface_loss_weight)

        loss = loss + loss_i.sum(-1).mean()

        return loss
