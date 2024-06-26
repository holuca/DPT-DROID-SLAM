import torch
import lietorch
import numpy as np
import argparse
import torch.nn.functional as F

import matplotlib.pyplot as plt
from lietorch import SE3
from modules.corr import CorrBlock, AltCorrBlock
import geom.projective_ops as pops
from models.dense_optical_tracking import DenseOpticalTracker, PointTracker, OpticalFlow
from droid_slam.utils.options.base_options import BaseOptions
from utils.torch import get_grid


class FactorGraph:
    def __init__(self, video, args, device="cuda:0", corr_impl="volume", max_factors=-1, upsample=False):
        self.video = video
        self.device = device
        self.max_factors = max_factors
        self.corr_impl = corr_impl
        self.upsample = upsample

        self.args = args
        

        # operator at 1/8 resolution
        self.ht = ht = video.ht // 8
        self.wd = wd = video.wd // 8
        self.point_tracker = PointTracker(self.ht, 
                                          self.wd).to(device=device)
        self.optical_flow_refiner = OpticalFlow(self.ht, 
                                                self.wd).to(device=device)

        self.coords0 = pops.coords_grid(ht, wd, device=device)
        self.ii = torch.as_tensor([], dtype=torch.long, device=device)
        self.jj = torch.as_tensor([], dtype=torch.long, device=device)
        self.age = torch.as_tensor([], dtype=torch.long, device=device)

        self.corr, self.net, self.inp = None, None, None
        self.damping = 1e-6 * torch.ones_like(self.video.disps)

        self.coarse_flow = torch.zeros([1, 0, ht//4, wd//4, 2], device=device, dtype=torch.float)
        self.target = torch.zeros([1, 0, ht, wd, 2], device=device, dtype=torch.float)
        self.weight = torch.zeros([1, 0, ht, wd, 2], device=device, dtype=torch.float)
        self.init = None


        # inactive factors
        self.ii_inac = torch.as_tensor([], dtype=torch.long, device=device)
        self.jj_inac = torch.as_tensor([], dtype=torch.long, device=device)
        self.ii_bad = torch.as_tensor([], dtype=torch.long, device=device)
        self.jj_bad = torch.as_tensor([], dtype=torch.long, device=device)

        self.target_inac = torch.zeros([1, 0, ht, wd, 2], device=device, dtype=torch.float)
        self.weight_inac = torch.zeros([1, 0, ht, wd, 2], device=device, dtype=torch.float)
    

    def __filter_repeated_edges(self, ii, jj):
        """ remove duplicate edges """

        keep = torch.zeros(ii.shape[0], dtype=torch.bool, device=ii.device)
        eset = set(
            [(i.item(), j.item()) for i, j in zip(self.ii, self.jj)] +
            [(i.item(), j.item()) for i, j in zip(self.ii_inac, self.jj_inac)])

        for k, (i, j) in enumerate(zip(ii, jj)):
            keep[k] = (i.item(), j.item()) not in eset

        return ii[keep], jj[keep]

    def print_edges(self):
        ii = self.ii.cpu().numpy()
        jj = self.jj.cpu().numpy()

        ix = np.argsort(ii)
        ii = ii[ix]
        jj = jj[ix]

        w = torch.mean(self.weight, dim=[0,2,3,4]).cpu().numpy()
        w = w[ix]
        for e in zip(ii, jj, w):
            print(e)
        print()

    def filter_edges(self):
        """ remove bad edges """
        conf = torch.mean(self.weight, dim=[0,2,3,4])
        mask = (torch.abs(self.ii-self.jj) > 2) & (conf < 0.001)

        self.ii_bad = torch.cat([self.ii_bad, self.ii[mask]])
        self.jj_bad = torch.cat([self.jj_bad, self.jj[mask]])
        self.rm_factors(mask, store=False)

    def clear_edges(self):
        self.rm_factors(self.ii >= 0)
        self.net = None
        self.inp = None

    @torch.cuda.amp.autocast(enabled=True)
    def add_factors(self, ii, jj, remove=False):
        """ add edges to factor graph """

        if not isinstance(ii, torch.Tensor):
            ii = torch.as_tensor(ii, dtype=torch.long, device=self.device)

        if not isinstance(jj, torch.Tensor):
            jj = torch.as_tensor(jj, dtype=torch.long, device=self.device)

        # remove duplicate edges
        ii, jj = self.__filter_repeated_edges(ii, jj)


        if ii.shape[0] == 0:
            return

        # place limit on number of factors
        if self.max_factors > 0 and self.ii.shape[0] + ii.shape[0] > self.max_factors \
                and self.corr is not None and remove:
            
            ix = torch.arange(len(self.age))[torch.argsort(self.age).cpu()]
            self.rm_factors(ix >= self.max_factors - ii.shape[0], store=True)

        net = self.video.nets[ii].to(self.device).unsqueeze(0)

        # correlation volume for new edges
        if self.corr_impl == "volume":
            c = (ii == jj).long()
            fmap1 = self.video.fmaps[ii,0].to(self.device).unsqueeze(0)
            fmap2 = self.video.fmaps[jj,c].to(self.device).unsqueeze(0)
            corr = CorrBlock(fmap1, fmap2)
            self.corr = corr if self.corr is None else self.corr.cat(corr)

            inp = self.video.inps[ii].to(self.device).unsqueeze(0)
            self.inp = inp if self.inp is None else torch.cat([self.inp, inp], 1)

        with torch.cuda.amp.autocast(enabled=False):
            #adding the flow from dot
            ii_cpu = ii.cpu().numpy()
            jj_cpu = jj.cpu().numpy()
            min_frame = min(np.min(ii_cpu),np.min(jj_cpu))
            max_frame = max(np.max(ii_cpu),np.max(jj_cpu))

            # We need at least 5 frames for dot
            if (max_frame - min_frame) <= 5:
                min_frame = max_frame - 5
                
            ii_cpu = ii_cpu - min_frame
            jj_cpu = jj_cpu - min_frame

            data = {"video":self.video.imagesdot[min_frame:max_frame+1,:,:,:][None]}
            target = self.dot_predict_flow(data,ii_cpu,jj_cpu)

            weight = torch.zeros_like(target)

        self.ii = torch.cat([self.ii, ii], 0)
        self.jj = torch.cat([self.jj, jj], 0)
        self.age = torch.cat([self.age, torch.zeros_like(ii)], 0)

        # reprojection factors
        self.net = net if self.net is None else torch.cat([self.net, net], 1)
        self.target = torch.cat([self.target, target], 1)
        self.weight = torch.cat([self.weight, weight], 1)

    def dot_predict_flow(self,data,ii,jj):
        "calculating the flow with DOT"
        video = data["video"]

        video_size_adjustement = 4
        
        B, T, C, H, W = data["video"].shape
        tracks = []
        sparse_points = []

        init = self.point_tracker(data, mode= "tracks_at_motion_boundaries" )["tracks"]
        init = torch.stack([init[..., 0] / (W- 1), init[..., 1] / (H - 1), init[..., 2]], dim=-1)

        #Downscale the grid 
        H_grid = int(H/video_size_adjustement)
        W_grid = int(W/video_size_adjustement)
        grid = get_grid(H_grid, W_grid, device=video.device)
        grid[..., 0] *= (W_grid - 1)
        grid[..., 1] *= (H_grid - 1)

        for x in range(len(ii)):
            sub_data = {
                "src_frame": data["video"][:, ii[x]],
                "tgt_frame": data["video"][:, jj[x]],
                "src_points": init[:, ii[x]],
                "tgt_points": init[:, jj[x]]
            }
            pred = self.optical_flow_refiner(sub_data, mode="flow_with_tracks_init")
            flow, alpha,coarse_flow = pred["flow"], pred["alpha"], pred["coarse_flow"]
            
            #Scaling the Flow to the desired Video//8 size
            flow1 = flow[0,:,:,0]/video_size_adjustement
            flow1 = flow1.unsqueeze(0).unsqueeze(0)
            flow2 = flow[0,:,:,1]/video_size_adjustement
            flow2 = flow2.unsqueeze(0).unsqueeze(0)
            flow1 = F.interpolate(flow1, size=(H_grid,W_grid), mode='bilinear')
            flow1 = flow1.squeeze(0).squeeze(0)
            flow2 = F.interpolate(flow2, size=(H_grid,W_grid), mode="bilinear")
            flow1 = flow1.squeeze(0).squeeze(0)
            flow1 = flow1.view(H_grid, W_grid, 1)
            flow2 = flow2.view(H_grid, W_grid, 1)
            flow = torch.cat((flow1,flow2), dim=2)

            #Adding the grid
            flow = (flow + grid).unsqueeze(0)

            tracks.append(flow)
            sparse_points.append(coarse_flow)
        tracks = torch.stack(tracks, dim=1)
        tracks = tracks.view(1, len(ii), H_grid, W_grid, 2)


        return tracks
    
    @torch.cuda.amp.autocast(enabled=True)
    def rm_factors(self, mask, store=False):
        """ drop edges from factor graph """

        # store estimated factors
        if store:
            self.ii_inac = torch.cat([self.ii_inac, self.ii[mask]], 0)
            self.jj_inac = torch.cat([self.jj_inac, self.jj[mask]], 0)
            self.target_inac = torch.cat([self.target_inac, self.target[:,mask]], 1)
            self.weight_inac = torch.cat([self.weight_inac, self.weight[:,mask]], 1)

        self.ii = self.ii[~mask]
        self.jj = self.jj[~mask]
        self.age = self.age[~mask]
        
        if self.corr_impl == "volume":
            self.corr = self.corr[~mask]

        if self.net is not None:
            self.net = self.net[:,~mask]

        if self.inp is not None:
            self.inp = self.inp[:,~mask]

        self.target = self.target[:,~mask]
        self.weight = self.weight[:,~mask]


    @torch.cuda.amp.autocast(enabled=True)
    def rm_keyframe(self, ix):
        """ drop edges from factor graph """


        with self.video.get_lock():
            self.video.images[ix] = self.video.images[ix+1]
            self.video.imagesdot[ix] = self.video.imagesdot[ix+1]
            self.video.poses[ix] = self.video.poses[ix+1]
            self.video.disps[ix] = self.video.disps[ix+1]
            self.video.disps_sens[ix] = self.video.disps_sens[ix+1]
            self.video.intrinsics[ix] = self.video.intrinsics[ix+1]

            self.video.nets[ix] = self.video.nets[ix+1]
            self.video.inps[ix] = self.video.inps[ix+1]
            self.video.fmaps[ix] = self.video.fmaps[ix+1]

        m = (self.ii_inac == ix) | (self.jj_inac == ix)
        self.ii_inac[self.ii_inac >= ix] -= 1
        self.jj_inac[self.jj_inac >= ix] -= 1

        if torch.any(m):
            self.ii_inac = self.ii_inac[~m]
            self.jj_inac = self.jj_inac[~m]
            self.target_inac = self.target_inac[:,~m]
            self.weight_inac = self.weight_inac[:,~m]

        m = (self.ii == ix) | (self.jj == ix)

        self.ii[self.ii >= ix] -= 1
        self.jj[self.jj >= ix] -= 1
        self.rm_factors(m, store=False)

    def local_variance(self,flow, kernel_size=3):
        "calculating the local variance"
        flow_u = flow[:,:,0]
        flow_v = flow[:,:,1]
        gaussian_kernel = torch.ones((1, 1, kernel_size, kernel_size), dtype=torch.float32) / (kernel_size * kernel_size)
        mean_flow_u = F.conv2d(flow_u.unsqueeze(0).unsqueeze(0), gaussian_kernel, padding=kernel_size//2).squeeze()
        sqr_mean_flow_u = F.conv2d((flow_u**2).unsqueeze(0).unsqueeze(0), gaussian_kernel, padding=kernel_size//2).squeeze()
        variance_u = sqr_mean_flow_u - mean_flow_u**2
        confidence_weights_u = 1 / (variance_u + 1e-5)  # Add epsilon to avoid division by zero
        confidence_weights_u /= variance_u.max()

        # Compute local variance for v component
        mean_flow_v = F.conv2d(flow_v.unsqueeze(0).unsqueeze(0), gaussian_kernel, padding=kernel_size//2).squeeze()
        sqr_mean_flow_v = F.conv2d((flow_v**2).unsqueeze(0).unsqueeze(0), gaussian_kernel, padding=kernel_size//2).squeeze()
        variance_v = sqr_mean_flow_v - mean_flow_v**2
        confidence_weights_v = 1 / (variance_v + 1e-5)  # Add epsilon to avoid division by zero
        confidence_weights_v /= variance_v.max()
        # Combine variances into a single tensor
        return torch.stack([confidence_weights_u, confidence_weights_v], dim=-1)
        
    @torch.cuda.amp.autocast(enabled=True)
    def get_confidence_weights_and_damping(self):
        "generating the confidence weights"
        num_frames = self.target.shape[1]
        ht, wd = self.target.shape[2:4]
        confidence_weights = []
        for frame in range(num_frames):
            target_frame = self.target[0, frame].to("cpu")
            frame_confidence = self.local_variance(target_frame)
            confidence_weights.append(frame_confidence)
        confidence_weights = torch.stack(confidence_weights).to(dtype=torch.float32).to(self.device).view(1, num_frames, ht, wd, 2)
        self.weight = confidence_weights.to(dtype=torch.float).to(self.device)
        return

    @torch.cuda.amp.autocast(enabled=True)
    def update(self, t0=None, t1=None, itrs=2, use_inactive=False, EP=1e-7, motion_only=False, old_version=False):
        """ run update operator on factor graph """


        if t0 is None:
            t0 = max(1, self.ii.min().item()+1)

        with torch.cuda.amp.autocast(enabled=False):

            self.get_confidence_weights_and_damping()

            if use_inactive:
                m = (self.ii_inac >= t0 - 3) & (self.jj_inac >= t0 - 3)
                ii = torch.cat([self.ii_inac[m], self.ii], 0)
                jj = torch.cat([self.jj_inac[m], self.jj], 0)
                target = torch.cat([self.target_inac[:,m], self.target], 1)
                weight = torch.cat([self.weight_inac[:,m], self.weight], 1)

            else:
                ii, jj, target, weight = self.ii, self.jj, self.target, self.weight



            damping = .2 * self.damping[torch.unique(ii)].contiguous() + EP
            ht, wd = self.coords0.shape[0:2]
            target = target.view(-1, ht, wd, 2).permute(0,3,1,2).contiguous()
            weight = weight.view(-1, ht, wd, 2).permute(0,3,1,2).contiguous()


            # dense bundle adjustment
            self.video.ba(target, weight, damping, ii, jj, t0, t1, 
                itrs=itrs, lm=1e-4, ep=0.1, motion_only=motion_only)
            
            upmask = torch.ones((8, 1, 8*8*9,ht,wd), device=self.device)*0.0000001

            if self.upsample:
                self.video.upsample(torch.unique(self.ii), upmask)

        self.age += 1



    def add_neighborhood_factors(self, t0, t1, r=3):
        """ add edges between neighboring frames within radius r """

        ii, jj = torch.meshgrid(torch.arange(t0,t1), torch.arange(t0,t1))
        ii = ii.reshape(-1).to(dtype=torch.long, device=self.device)
        jj = jj.reshape(-1).to(dtype=torch.long, device=self.device)

        c = 1 if self.video.stereo else 0

        keep = ((ii - jj).abs() > c) & ((ii - jj).abs() <= r)
        self.add_factors(ii[keep], jj[keep])

    
    def add_proximity_factors(self, t0=0, t1=0, rad=2, nms=2, beta=0.25, thresh=16.0, remove=False):
        """ add edges to the factor graph based on distance """

        t = self.video.counter.value
        ix = torch.arange(t0, t)
        jx = torch.arange(t1, t)

        ii, jj = torch.meshgrid(ix, jx)
        ii = ii.reshape(-1)
        jj = jj.reshape(-1)

        d = self.video.distance(ii, jj, beta=beta)
        d[ii - rad < jj] = np.inf
        d[d > 100] = np.inf

        ii1 = torch.cat([self.ii, self.ii_bad, self.ii_inac], 0)
        jj1 = torch.cat([self.jj, self.jj_bad, self.jj_inac], 0)
        for i, j in zip(ii1.cpu().numpy(), jj1.cpu().numpy()):
            for di in range(-nms, nms+1):
                for dj in range(-nms, nms+1):
                    if abs(di) + abs(dj) <= max(min(abs(i-j)-2, nms), 0):
                        i1 = i + di
                        j1 = j + dj

                        if (t0 <= i1 < t) and (t1 <= j1 < t):
                            d[(i1-t0)*(t-t1) + (j1-t1)] = np.inf


        es = []
        for i in range(t0, t):
            if self.video.stereo:
                es.append((i, i))
                d[(i-t0)*(t-t1) + (i-t1)] = np.inf

            for j in range(max(i-rad-1,0), i):
                es.append((i,j))
                es.append((j,i))
                d[(i-t0)*(t-t1) + (j-t1)] = np.inf

        ix = torch.argsort(d)
        for k in ix:
            if d[k].item() > thresh:
                continue

            if len(es) > self.max_factors:
                break

            i = ii[k]
            j = jj[k]
            
            # bidirectional
            es.append((i, j))
            es.append((j, i))

            for di in range(-nms, nms+1):
                for dj in range(-nms, nms+1):
                    if abs(di) + abs(dj) <= max(min(abs(i-j)-2, nms), 0):
                        i1 = i + di
                        j1 = j + dj

                        if (t0 <= i1 < t) and (t1 <= j1 < t):
                            d[(i1-t0)*(t-t1) + (j1-t1)] = np.inf

        ii, jj = torch.as_tensor(es, device=self.device).unbind(dim=-1)
        self.add_factors(ii, jj, remove)
