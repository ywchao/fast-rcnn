# --------------------------------------------------------
# Fast R-CNN
# Copyright (c) 2015 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Ross Girshick
# --------------------------------------------------------

"""Compute minibatch blobs for training a Fast R-CNN network."""

import numpy as np
import numpy.random as npr
import cv2
from fast_rcnn.config import cfg
from utils.blob import prep_im_for_blob, im_list_to_blob

def get_minibatch(roidb, num_classes):
    """Given a roidb, construct a minibatch sampled from it."""
    num_images = len(roidb)
    # Sample random scales to use for each image in this batch
    random_scale_inds = npr.randint(0, high=len(cfg.TRAIN.SCALES),
                                    size=num_images)
    assert(cfg.TRAIN.BATCH_SIZE % num_images == 0), \
        'num_images ({}) must divide BATCH_SIZE ({})'. \
        format(num_images, cfg.TRAIN.BATCH_SIZE)
    rois_per_image = cfg.TRAIN.BATCH_SIZE / num_images
    fg_rois_per_image = np.round(cfg.TRAIN.FG_FRACTION * rois_per_image)

    if cfg.FLAG_HICO:
        assert(not cfg.TRAIN.BBOX_REG)
        assert(cfg.TRAIN.FG_FRACTION == 1)
        assert(cfg.TRAIN.BATCH_SIZE == num_images)
        assert(cfg.TRAIN.BATCH_SIZE == cfg.TRAIN.BATCH_SIZE)

    # Get the input image blob, formatted for caffe
    #   w_org/h_org: image width/height in the original scale; for the 4-side
    #                context feature
    im_blob, im_scales, w_org, h_org = _get_image_blob(roidb, random_scale_inds)

    # Now, build the region of interest and label blobs
    if cfg.FEAT_TYPE == 4:
        # 4-side context feature
        rois_blob = [np.zeros((0, 5), dtype=np.float32)] * cfg.TOP_K * 4
    else:
        rois_blob = [np.zeros((0, 5), dtype=np.float32)] * cfg.TOP_K
    labels_blob = np.zeros((0), dtype=np.float32)
    bbox_targets_blob = np.zeros((0, 4 * num_classes), dtype=np.float32)
    bbox_loss_blob = np.zeros(bbox_targets_blob.shape, dtype=np.float32)
    # all_overlaps = []
    for im_i in xrange(num_images):
        im_rois = [0] * cfg.TOP_K
        if cfg.FEAT_TYPE == 4:
            im_rois = im_rois * 4
        if not cfg.FLAG_HICO:
            assert(cfg.TOP_K == 1 and cfg.FEAT_TYPE == 0)
            labels, overlaps, im_rois[0], bbox_targets, bbox_loss \
                = _sample_rois(roidb[im_i], fg_rois_per_image, rois_per_image,
                               num_classes)
        else:
            for ind in xrange(cfg.TOP_K):
                # 'boxes' are sorted by detection scores already
                if cfg.FEAT_TYPE == 4:
                     # get 4 side boxes
                     im_rois[ind*4+0], im_rois[ind*4+1], \
                     im_rois[ind*4+2], im_rois[ind*4+3], \
                         = _get_4_side_bbox(roidb[im_i]['boxes'][ind,:], 
                                            w_org[im_i], 
                                            h_org[im_i])
                else:
                    # get tight bbox
                    im_rois[ind] = roidb[im_i]['boxes'][ind:ind+1,:]
            labels  = roidb[im_i]['label'][0:1]
            bbox_targets = np.zeros((0, 4 * num_classes), dtype=np.float32)
            bbox_loss = np.zeros(bbox_targets.shape, dtype=np.float32)

        # Add to RoIs blob
        for ind in xrange(len(im_rois)):
            rois = _project_im_rois(im_rois[ind], im_scales[im_i])
            batch_ind = im_i * np.ones((rois.shape[0], 1))
            rois_blob_this_image = np.hstack((batch_ind, rois))
            rois_blob[ind] = np.vstack((rois_blob[ind], rois_blob_this_image))

        # Add to labels, bbox targets, and bbox loss blobs
        labels_blob = np.hstack((labels_blob, labels))
        bbox_targets_blob = np.vstack((bbox_targets_blob, bbox_targets))
        bbox_loss_blob = np.vstack((bbox_loss_blob, bbox_loss))
        # all_overlaps = np.hstack((all_overlaps, overlaps))

    # For debug visualizations
    # _vis_minibatch(im_blob, rois_blob, labels_blob, all_overlaps)

    blobs = {'data': im_blob,
             'rois': rois_blob[0],
             'labels': labels_blob}

    # for HICO experiments
    if cfg.FLAG_HICO:
        # does not suppoart bbox regression
        assert(cfg.TRAIN.BBOX_REG == False)
        blobs = {'data': im_blob,
                 'labels': labels_blob}
        for ind in xrange(cfg.TOP_K):
            if cfg.FEAT_TYPE == 4:
                for i, s in enumerate(['l','t','r','b']):
                    # change _name_to_top_map
                    key = 'rois_%d_%s' % (ind+1,s)
                    blobs[key] = rois_blob[ind*4+i]
            else:
                # change _name_to_top_map
                key = 'rois_%d' % (ind+1)
                blobs[key] = rois_blob[ind]

    if cfg.TRAIN.BBOX_REG:
        blobs['bbox_targets'] = bbox_targets_blob
        blobs['bbox_loss_weights'] = bbox_loss_blob

    return blobs

def _sample_rois(roidb, fg_rois_per_image, rois_per_image, num_classes):
    """Generate a random sample of RoIs comprising foreground and background
    examples.
    """
    # label = class RoI has max overlap with
    labels = roidb['max_classes']
    overlaps = roidb['max_overlaps']
    rois = roidb['boxes']

    # Select foreground RoIs as those with >= FG_THRESH overlap
    fg_inds = np.where(overlaps >= cfg.TRAIN.FG_THRESH)[0]
    # Guard against the case when an image has fewer than fg_rois_per_image
    # foreground RoIs
    fg_rois_per_this_image = np.minimum(fg_rois_per_image, fg_inds.size)
    # Sample foreground regions without replacement
    if fg_inds.size > 0:
        fg_inds = npr.choice(fg_inds, size=fg_rois_per_this_image,
                             replace=False)

    # Select background RoIs as those within [BG_THRESH_LO, BG_THRESH_HI)
    bg_inds = np.where((overlaps < cfg.TRAIN.BG_THRESH_HI) &
                       (overlaps >= cfg.TRAIN.BG_THRESH_LO))[0]
    # Compute number of background RoIs to take from this image (guarding
    # against there being fewer than desired)
    bg_rois_per_this_image = rois_per_image - fg_rois_per_this_image
    bg_rois_per_this_image = np.minimum(bg_rois_per_this_image,
                                        bg_inds.size)
    # Sample foreground regions without replacement
    if bg_inds.size > 0:
        bg_inds = npr.choice(bg_inds, size=bg_rois_per_this_image,
                             replace=False)

    # The indices that we're selecting (both fg and bg)
    keep_inds = np.append(fg_inds, bg_inds)
    # Select sampled values from various arrays:
    labels = labels[keep_inds]
    # Clamp labels for the background RoIs to 0
    labels[fg_rois_per_this_image:] = 0
    overlaps = overlaps[keep_inds]
    rois = rois[keep_inds]

    bbox_targets, bbox_loss_weights = \
            _get_bbox_regression_labels(roidb['bbox_targets'][keep_inds, :],
                                        num_classes)

    return labels, overlaps, rois, bbox_targets, bbox_loss_weights

def _get_4_side_bbox(bbox, im_width, im_height):
    assert(bbox.ndim == 1 and bbox.shape[0] == 4)
    # get radius
    w = bbox[2]-bbox[0]+1;
    h = bbox[3]-bbox[1]+1;
    r = (w+h)/2;
    # get boxes
    bbox_l = np.array([np.maximum(bbox[0]-0.5*r,1),
                       bbox[1],
                       bbox[2]-0.5*w,
                       bbox[3]])
    bbox_t = np.array([bbox[0],
                       np.maximum(bbox[1]-0.5*h,1),
                       bbox[2],
                       bbox[3]-0.5*h])
    bbox_r = np.array([bbox[0]+0.5*w,
                       bbox[1],
                       np.minimum(bbox[2]+0.5*r,im_width),
                       bbox[3]])
    bbox_b = np.array([bbox[0],
                       bbox[1]+0.5*h,
                       bbox[2],
                       np.minimum(bbox[3]+0.5*h,im_height)])
    # bbox_l = np.around(bbox_l).astype('uint16')
    # bbox_t = np.around(bbox_t).astype('uint16')
    # bbox_r = np.around(bbox_r).astype('uint16')
    # bbox_b = np.around(bbox_b).astype('uint16')

    # return in the order left, top, right, bottom
    return bbox_l[None,:], bbox_t[None,:], bbox_r[None,:], bbox_b[None,:]

def _get_image_blob(roidb, scale_inds):
    """Builds an input blob from the images in the roidb at the specified
    scales.
    """
    num_images = len(roidb)
    processed_ims = []
    im_scales = []
    w_org = []
    h_org = []
    for i in xrange(num_images):
        im = cv2.imread(roidb[i]['image'])
        # get width and height in the original scale
        w_org.append(im.shape[1])
        h_org.append(im.shape[0])
        if roidb[i]['flipped']:
            im = im[:, ::-1, :]
        target_size = cfg.TRAIN.SCALES[scale_inds[i]]
        im, im_scale = prep_im_for_blob(im, cfg.PIXEL_MEANS, target_size,
                                        cfg.TRAIN.MAX_SIZE)
        im_scales.append(im_scale)
        processed_ims.append(im)

    # Create a blob to hold the input images
    blob = im_list_to_blob(processed_ims)

    return blob, im_scales, w_org, h_org

def _project_im_rois(im_rois, im_scale_factor):
    """Project image RoIs into the rescaled training image."""
    rois = im_rois * im_scale_factor
    return rois

def _get_bbox_regression_labels(bbox_target_data, num_classes):
    """Bounding-box regression targets are stored in a compact form in the
    roidb.

    This function expands those targets into the 4-of-4*K representation used
    by the network (i.e. only one class has non-zero targets). The loss weights
    are similarly expanded.

    Returns:
        bbox_target_data (ndarray): N x 4K blob of regression targets
        bbox_loss_weights (ndarray): N x 4K blob of loss weights
    """
    clss = bbox_target_data[:, 0]
    bbox_targets = np.zeros((clss.size, 4 * num_classes), dtype=np.float32)
    bbox_loss_weights = np.zeros(bbox_targets.shape, dtype=np.float32)
    inds = np.where(clss > 0)[0]
    for ind in inds:
        cls = clss[ind]
        start = 4 * cls
        end = start + 4
        bbox_targets[ind, start:end] = bbox_target_data[ind, 1:]
        bbox_loss_weights[ind, start:end] = [1., 1., 1., 1.]
    return bbox_targets, bbox_loss_weights

def _vis_minibatch(im_blob, rois_blob, labels_blob, overlaps):
    """Visualize a mini-batch for debugging."""
    import matplotlib.pyplot as plt
    for i in xrange(rois_blob.shape[0]):
        rois = rois_blob[i, :]
        im_ind = rois[0]
        roi = rois[1:]
        im = im_blob[im_ind, :, :, :].transpose((1, 2, 0)).copy()
        im += cfg.PIXEL_MEANS
        im = im[:, :, (2, 1, 0)]
        im = im.astype(np.uint8)
        cls = labels_blob[i]
        plt.imshow(im)
        print 'class: ', cls, ' overlap: ', overlaps[i]
        plt.gca().add_patch(
            plt.Rectangle((roi[0], roi[1]), roi[2] - roi[0],
                          roi[3] - roi[1], fill=False,
                          edgecolor='r', linewidth=3)
            )
        plt.show()
