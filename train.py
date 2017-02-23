# Some of this code came from the https://github.com/tensorflow/models/tree/master/slim
# directory, so lets keep the Google license around for now.
#
# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import copy
import os

import numpy as np
import tensorflow as tf
import tensorflow.contrib.slim as slim

from config.parse_config import parse_config_file
from nets import nets_factory
from preprocessing import inputs


def _configure_learning_rate(global_step, cfg):
    """Configures the learning rate.
    Args:
        num_samples_per_epoch: The number of samples in each epoch of training.
        global_step: The global_step tensor.
    Returns:
        A `Tensor` representing the learning rate.
    Raises:
        ValueError: if cfg.LEARNING_RATE_DECAY_TYPE is not recognized.
    """


    decay_steps = int(cfg.NUM_TRAIN_EXAMPLES / cfg.BATCH_SIZE * cfg.NUM_EPOCHS_PER_DELAY)

    if cfg.LEARNING_RATE_DECAY_TYPE == 'exponential':
        return tf.train.exponential_decay(cfg.INITIAL_LEARNING_RATE,
                                          global_step,
                                          decay_steps,
                                          cfg.LEARNING_RATE_DECAY_FACTOR,
                                          staircase=cfg.LEARNING_RATE_STAIRCASE,
                                          name='exponential_decay_learning_rate')

    elif cfg.LEARNING_RATE_DECAY_TYPE == 'fixed':
        return tf.constant(cfg.INITIAL_LEARNING_RATE, name='fixed_learning_rate')

    elif cfg.LEARNING_RATE_DECAY_TYPE == 'polynomial':
        return tf.train.polynomial_decay(cfg.INITIAL_LEARNING_RATE,
                                         global_step,
                                         decay_steps,
                                         cfg.END_LEARNING_RATE,
                                         power=1.0,
                                         cycle=False,
                                         name='polynomial_decay_learning_rate')
    else:
        raise ValueError('learning_rate_decay_type [%s] was not recognized',
                         cfg.LEARNING_RATE_DECAY_TYPE)


def _configure_optimizer(learning_rate, cfg):
    """Configures the optimizer used for training.
    Args:
        learning_rate: A scalar or `Tensor` learning rate.
    Returns:
        An instance of an optimizer.
    Raises:
        ValueError: if FLAGS.optimizer is not recognized.
    """
    if cfg.OPTIMIZER == 'adadelta':
        optimizer = tf.train.AdadeltaOptimizer(
            learning_rate,
            rho=cfg.ADADELTA_RHO,
            epsilon=cfg.OPTIMIZER_EPSILON)
    elif cfg.OPTIMIZER == 'adagrad':
        optimizer = tf.train.AdagradOptimizer(
            learning_rate,
            initial_accumulator_value=cfg.ADAGRAD_INITIAL_ACCUMULATOR_VALUE)
    elif cfg.OPTIMIZER == 'adam':
        optimizer = tf.train.AdamOptimizer(
            learning_rate,
            beta1=cfg.ADAM_BETA1,
            beta2=cfg.ADAM_BETA2,
            epsilon=cfg.OPTIMIZER_EPSILON)
    elif cfg.OPTIMIZER == 'ftrl':
        optimizer = tf.train.FtrlOptimizer(
            learning_rate,
            learning_rate_power=cfg.FTRL_LEARNING_RATE_POWER,
            initial_accumulator_value=cfg.FTRL_INITIAL_ACCUMULATOR_VALUE,
            l1_regularization_strength=cfg.FTRL_L1,
            l2_regularization_strength=cfg.FTRL_L2)
    elif cfg.OPTIMIZER == 'momentum':
        optimizer = tf.train.MomentumOptimizer(
            learning_rate,
            momentum=cfg.MOMENTUM,
            name='Momentum')
    elif cfg.OPTIMIZER == 'rmsprop':
        optimizer = tf.train.RMSPropOptimizer(
            learning_rate,
            decay=cfg.RMSPROP_DECAY,
            momentum=cfg.MOMENTUM,
            epsilon=cfg.OPTIMIZER_EPSILON)
    elif cfg.OPTIMIZER == 'sgd':
        optimizer = tf.train.GradientDescentOptimizer(learning_rate)
    else:
        raise ValueError('Optimizer [%s] was not recognized', cfg.OPTIMIZER)
    return optimizer

def get_trainable_variables(trainable_scopes):
    """Returns a list of variables to train.
    Returns:
        A list of variables to train by the optimizer.
    """

    if trainable_scopes is None:
        return tf.trainable_variables()

    trainable_scopes = [scope.strip() for scope in trainable_scopes]

    variables_to_train = []
    for scope in trainable_scopes:
        variables = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope)
        variables_to_train.extend(variables)
    return variables_to_train


def get_init_function(logdir, pretrained_model_path, checkpoint_exclude_scopes):
    """
    Args:
        logdir : location of where we will be storing checkpoint files.
        pretrained_model_path : a path to a specific model, or a directory with a checkpoint file. The latest model will be used.
        fine_tune : If True, then the detection heads will not be restored.
        original_inception_vars : A list of variables that do not include the detection heads.
        use_moving_averages : If True, then the moving average values of the variables will be restored.
        restore_moving_averages : If True, then the moving average values will also be restored.
        ema : The exponential moving average object
    """


    if pretrained_model_path is None:
        return None

    # Warn the user if a checkpoint exists in the train_dir. Then we'll be
    # ignoring the checkpoint anyway.
    if tf.train.latest_checkpoint(logdir):
        tf.logging.info(
            'Ignoring --pretrained_model_path because a checkpoint already exists in %s'
            % logdir)
        return None

    exclusions = []
    if checkpoint_exclude_scopes:
        exclusions = [scope.strip() for scope in checkpoint_exclude_scopes]

    # TODO(sguada) variables.filter_variables()
    variables_to_restore = []
    for var in slim.get_model_variables():
        excluded = False
        for exclusion in exclusions:
          if var.op.name.startswith(exclusion):
            excluded = True
            break
        if not excluded:
          variables_to_restore.append(var)

    if os.path.isdir(pretrained_model_path):
        checkpoint_path = tf.train.latest_checkpoint(pretrained_model_path)
        if checkpoint_path is None:
            raise ValueError(
                "No model checkpoint file found in directory %s" % (pretrained_model_path))

    else:
        checkpoint_path = pretrained_model_path

    tf.logging.info('Fine-tuning from %s' % checkpoint_path)

    return slim.assign_from_checkpoint_fn(
        checkpoint_path,
        variables_to_restore,
        ignore_missing_vars=False)


def train(tfrecords, logdir, cfg, pretrained_model_path=None, trainable_scopes=None, checkpoint_exclude_scopes=None):
    """
    Args:
        tfrecords (list)
        bbox_priors (np.array)
        logdir (str)
        cfg (EasyDict)
        pretrained_model_path (str) : path to a pretrained Inception Network
    """
    tf.logging.set_verbosity(tf.logging.DEBUG)

    graph = tf.Graph()

    # Force all Variables to reside on the CPU.
    with graph.as_default():

        # Create a variable to count the number of train() calls.
        global_step = slim.get_or_create_global_step()

        # Calculate the learning rate schedule.
        lr = _configure_learning_rate(global_step, cfg)

        # Create an optimizer that performs gradient descent.
        optimizer = _configure_optimizer(lr, cfg)

        batch_dict = inputs.input_nodes(
            tfrecords=tfrecords,
            cfg=cfg.IMAGE_PROCESSING,
            num_epochs=None,
            batch_size=cfg.BATCH_SIZE,
            num_threads=cfg.NUM_INPUT_THREADS,
            shuffle_batch =cfg.SHUFFLE_QUEUE,
            random_seed=cfg.RANDOM_SEED,
            capacity=cfg.QUEUE_CAPACITY,
            min_after_dequeue=cfg.QUEUE_MIN,
            add_summaries=True,
            input_type='train'
        )

        batched_one_hot_labels = slim.one_hot_encoding(batch_dict['labels'],
                                                       num_classes=cfg.NUM_CLASSES)

        input_summaries = copy.copy(tf.get_collection(tf.GraphKeys.SUMMARIES))

        arg_scope = nets_factory.arg_scopes_map[cfg.MODEL_NAME](
            weight_decay=cfg.WEIGHT_DECAY,
            batch_norm_decay=cfg.BATCHNORM_MOVING_AVERAGE_DECAY,
            batch_norm_epsilon=cfg.BATCHNORM_EPSILON
        )

        with slim.arg_scope(arg_scope):
            logits, end_points = nets_factory.networks_map[cfg.MODEL_NAME](
                inputs=batch_dict['inputs'],
                num_classes=cfg.NUM_CLASSES,
                dropout_keep_prob=cfg.DROPOUT_KEEP_PROB,
                is_training=True
            )

            # Add the losses
            if 'AuxLogits' in end_points:
                tf.losses.softmax_cross_entropy(
                    logits=end_points['AuxLogits'], onehot_labels=batched_one_hot_labels,
                    label_smoothing=0., weights=0.4, scope='aux_loss')

            tf.losses.softmax_cross_entropy(
                logits=logits, onehot_labels=batched_one_hot_labels, label_smoothing=0., weights=1.0)

        total_loss = tf.losses.get_total_loss()

        # Track the moving averages of all trainable variables.
        # At test time we'll restore all variables with the average value
        # Note that we maintain a "double-average" of the BatchNormalization
        # global statistics. This is more complicated then need be but we employ
        # this for backward-compatibility with our previous models.
        ema = tf.train.ExponentialMovingAverage(
          decay=cfg.MOVING_AVERAGE_DECAY,
          num_updates=global_step
        )
        variables_to_average = (slim.get_model_variables())
        maintain_averages_op = ema.apply(variables_to_average)
        tf.add_to_collection(tf.GraphKeys.UPDATE_OPS, maintain_averages_op)

        trainable_vars = get_trainable_variables(trainable_scopes)
        train_op = slim.learning.create_train_op(total_loss, optimizer, variables_to_train=trainable_vars)

        # Summary operations
        summary_op = tf.summary.merge([
          tf.summary.scalar('total_loss', total_loss),
          tf.summary.scalar('learning_rate', lr)
        ] + input_summaries)

        sess_config = tf.ConfigProto(
          log_device_placement=cfg.SESSION_CONFIG.LOG_DEVICE_PLACEMENT,
          allow_soft_placement = True,
          gpu_options = tf.GPUOptions(
              per_process_gpu_memory_fraction=cfg.SESSION_CONFIG.PER_PROCESS_GPU_MEMORY_FRACTION
          )
        )

        saver = tf.train.Saver(
          # Save all variables
          max_to_keep = cfg.MAX_TO_KEEP,
          keep_checkpoint_every_n_hours = cfg.KEEP_CHECKPOINT_EVERY_N_HOURS
        )

        # Run training.
        slim.learning.train(train_op, logdir,
          init_fn=get_init_function(logdir, pretrained_model_path, checkpoint_exclude_scopes),
          number_of_steps=cfg.NUM_TRAIN_ITERATIONS,
          save_summaries_secs=cfg.SAVE_SUMMARY_SECS,
          save_interval_secs=cfg.SAVE_INTERVAL_SECS,
          saver=saver,
          session_config=sess_config,
          summary_op = summary_op,
          log_every_n_steps = cfg.LOG_EVERY_N_STEPS
        )

def parse_args():

    parser = argparse.ArgumentParser(description='Train the classification system')

    parser.add_argument('--tfrecords', dest='tfrecords',
                        help='paths to tfrecords files that contain the training data', type=str,
                        nargs='+', required=True)

    parser.add_argument('--logdir', dest='logdir',
                          help='path to directory to store summary files and checkpoint files', type=str,
                          required=True)

    parser.add_argument('--config', dest='config_file',
                        help='Path to the configuration file',
                        required=True, type=str)

    parser.add_argument('--pretrained_model', dest='pretrained_model',
                        help='Path to a model to restore. This is ignored if there is model in the logdir.',
                        required=False, type=str, default=None)

    parser.add_argument('--trainable_scopes', dest='trainable_scopes',
                        help='Only variables within these scopes will be trained.',
                        type=str, nargs='+', default=None, required=False)

    parser.add_argument('--checkpoint_exclude_scopes', dest='checkpoint_exclude_scopes',
                        help='Variables within these scopes will not be restored from the checkpoint files.',
                        type=str, nargs='+', default=None, required=False)

    parser.add_argument('--max_number_of_steps', dest='max_number_of_steps',
                        help='The maximum number of iterations to run.',
                        required=False, type=int, default=None)

    parser.add_argument('--learning_rate_decay_type', dest='learning_rate_decay_type',
                          help='Type of the decay', type=str,
                          required=False, default=None)

    parser.add_argument('--lr', dest='learning_rate',
                          help='Initial learning rate', type=float,
                          required=False, default=None)

    parser.add_argument('--batch_size', dest='batch_size',
                        help='The number of images in a batch.',
                        required=False, type=int, default=None)

    parser.add_argument('--model_name', dest='model_name',
                        help='The name of the architecture to use.',
                        required=False, type=str, default=None)

    args = parser.parse_args()
    return args

def main():
    args = parse_args()

    cfg = parse_config_file(args.config_file)

    # Replace cfg parameters with the command line values
    if args.max_number_of_steps != None:
        cfg.NUM_TRAIN_ITERATIONS = args.max_number_of_steps

    if args.learning_rate_decay_type != None:
        cfg.LEARNING_RATE_DECAY_TYPE = args.learning_rate_decay_type

    if args.learning_rate != None:
        cfg.INITIAL_LEARNING_RATE = args.learning_rate

    if args.batch_size != None:
        cfg.BATCH_SIZE = args.batch_size

    if args.model_name != None:
        cfg.MODEL_NAME = args.model_name

    train(
        tfrecords=args.tfrecords,
        logdir=args.logdir,
        cfg=cfg,
        pretrained_model_path=args.pretrained_model,
        trainable_scopes = args.trainable_scopes,
        checkpoint_exclude_scopes = args.checkpoint_exclude_scopes
    )

if __name__ == '__main__':
  main()