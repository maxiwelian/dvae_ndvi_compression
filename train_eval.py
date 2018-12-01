# Copyright 2018 D-Wave Systems Inc.
# DVAE# licensed to authorized users only under the applicable license
# agreement.  See LICENSE.

import time

import numpy as np
import tensorflow as tf
from six.moves import xrange
import os

from thirdparty import input_data

def save(path, x):
    np.save(path, x)

from util import create_reconstruction_image, tile_image_tf, Print, get_global_step_var


def evaluate(sess, neg_elbo, log_iw, input, dataset, batch_size, k_iw):
    """ This function evaluates negative elbo and negative log likelihood using tensors created in the vae class.
    
    Args:
        sess: tf sess.
        neg_elbo: a tensor containing neg_elbo for each element in the image placeholder.
        log_iw: a tensor containing log p(x, z) / q(z|x) for each element in the image placeholder.
        images_placeholder: a place holder that is used to feed test batches for computing neg_elbo and log_iw.
        data_set: a dataset object that can be used to generate test batches.
        batch_size: batch size used for evaluation.
        k_iw: K in the importance weighted log likelihood approximation.

    Returns:
        neg_elbo_value: the value of neg elbo computed on the whole test dataset.
        neg_log_likelihood: the value of neg log likelihood computed on the whole test dataset.
    """
    # And run one epoch of eval.
    steps_per_epoch = dataset.num_examples // batch_size    # number of mini-batches in the dataset.
    assert dataset.num_examples % batch_size == 0, 'batch size should divide the dataset size.'

    # compute importance weighted LL:
    total_ll, total_neg_elbo = 0, 0
    for step in xrange(steps_per_epoch):
        # feed_dict = fill_feed_dict(data_set, images_placeholder, batch_size)

        # will contain log_iw for each trial (sample from hiddens) for each datapoint in batch.
        log_iw_values = np.zeros((k_iw, batch_size), dtype=np.float128)
        neg_elbo_value_average = np.zeros((k_iw, ), dtype=np.float64)
        # compute log_iw using the same batch for k_iw times.
        for k in range(k_iw):
            neg_elbo_value_average[k], log_iw_values[k] = sess.run([neg_elbo, log_iw])

        # LL ~ log(1/k \sum_k  exp(log_iw_k))
        max_log_iw = np.mean(log_iw_values, axis=0)
        log_iw_values -= max_log_iw
        ll = np.log(np.mean(np.exp(log_iw_values), axis=0)) + max_log_iw
        # add average(LL) on the current batch to the total ll.
        total_ll += np.mean(ll)
        total_neg_elbo += np.mean(neg_elbo_value_average)

    neg_log_likelihood = - total_ll / steps_per_epoch
    neg_elbo_value = total_neg_elbo / steps_per_epoch

    return neg_elbo_value, neg_log_likelihood


def run_training(vae, cont_train, config_train, log_dir):
    """ The main function that will derive training of a vae.
    Args:
        vae: is an object from the class VAE. 
        cont_train: a boolean flag indicating whether train should continue from the checkpoint stored in the log_dir.
        config_train: a dictionary containing config. training (hyperparameters).
        log_dir: path to a directory that will used for storing both tensorboard files and checkpoints.

    Returns:
        test_neg_ll_value: the value of test log-likelihood.
    """
    sampler_name = config_train['sampler_name']
    use_iw = config_train['use_iw']
    Print('Starting training.')
    batch_size = config_train['batch_size']
    num_latent_units = config_train['num_latent_units']
    # Get the train, val, test sets of on MNIST.

    # place holder for input.
    # input_placeholder = tf.placeholder(tf.float32, shape=(None, vae.num_input))
    with tf.name_scope("data"):

        data_dir = config_train['data_dir']
        eval_batch_size = config_train['eval_batch_size']
        datasets = input_data.Datasets(data_dir, batch_size=batch_size, eval_batch_size=eval_batch_size, nTestYears=10,
                                       patch_size=28, scale=1e-4,  binarize_threshold=0.0)


        ## NDVI DATA -- move import up top later
        train_data = datasets.train.read_records()
        test_data = datasets.test.read_records()
        validation_data = datasets.validation.read_records()

        patch_size = 28

        # reshape from 4D to 2D
        train_data = tf.reshape(train_data, [-1, patch_size ** 2])
        test_data = tf.reshape(test_data, [-1, patch_size ** 2])
        validation_data = tf.reshape(validation_data, [-1, patch_size ** 2])

        input_tr = tf.cast(tf.greater(train_data, datasets.binarize_threshold), tf.float32)
        input_val = tf.cast(tf.greater(validation_data, datasets.binarize_threshold), tf.float32)
        input_test = tf.cast(tf.greater(test_data, datasets.binarize_threshold), tf.float32)

    # define training graph.
    if use_iw:
        Print('using IW obj. function')
        iw_loss, neg_elbo, sigmoid_output, wd_loss, _, post_samples = \
            vae.neg_elbo(input_tr, is_training=True, k=config_train['k'], use_iw=use_iw)
        loss = iw_loss + wd_loss
        # create scalar summary for training loss.
        tf.summary.scalar('train/neg_iw_loss', iw_loss)
        sigmoid_output = tf.slice(sigmoid_output, [0, 0], [batch_size, -1])
    else:
        Print('using VAE obj. function')
        _, neg_elbo, sigmoid_output, wd_loss, _, post_samples = \
            vae.neg_elbo(input_tr, is_training=True, k=config_train['k'], use_iw=use_iw)
        loss = neg_elbo + wd_loss
        # create scalar summary for training loss.
        tf.summary.scalar('train/neg_elbo', neg_elbo)

    train_op = vae.training(loss)

    # create images for reconstruction.
    image = create_reconstruction_image(input_tr, sigmoid_output[:batch_size], batch_size)
    tf.summary.image('recon', image, max_outputs=1)

    # define graph to generate random samples from model.
    num_samples = 100
    random_samples = vae.generate_samples(num_samples)
    tiled_samples = tile_image_tf(random_samples, n=int(np.sqrt(num_samples)), m=int(np.sqrt(num_samples)), width=28, height=28)
    tf.summary.image('generated_sample', tiled_samples, max_outputs=1)

    # merge all summary for training graph
    train_summary_op = tf.summary.merge_all()

    # define a parallel graph for evaluation. Enable parameter sharing by setting is_training to False.
    _, neg_elbo_eval, _, _, log_iw_eval, _ = vae.neg_elbo(input_test, is_training=False)

    # the following will create summaries that will be used in the evaluation graph.
    val_neg_elbo, test_neg_elbo = tf.placeholder(tf.float32, shape=()), tf.placeholder(tf.float32, shape=())
    val_neg_ll, test_neg_ll = tf.placeholder(tf.float32, shape=()), tf.placeholder(tf.float32, shape=())
    val_summary = tf.summary.scalar('val/neg_elbo', val_neg_elbo)
    test_summary = tf.summary.scalar('test/neg_elbo', test_neg_elbo)
    val_ll_summary = tf.summary.scalar('val/neg_ll', val_neg_ll)
    test_ll_summary = tf.summary.scalar('test/neg_ll', test_neg_ll)
    eval_summary_op = tf.summary.merge([val_summary, test_summary, val_ll_summary, test_ll_summary])

    # start checkpoint saver.
    saver = tf.train.Saver(max_to_keep=1)
    sess = tf.Session()

    # Run the Op to initialize the variables.
    if cont_train:
        ckpt = tf.train.get_checkpoint_state(log_dir)
        if ckpt and ckpt.model_checkpoint_path:
            saver.restore(sess, ckpt.model_checkpoint_path)
            init_step = int(ckpt.model_checkpoint_path.split('-')[-1]) + 1
            Print('Initializing model from %s from step %d' % (log_dir, init_step))
        else:
            raise('No Checkpoint was fount at %s' % log_dir)
    else:
        init = tf.global_variables_initializer()
        sess.run(init)
        init_step = 0

    # Instantiate a SummaryWriter to output summaries and the Graph.
    # Create train/validation/test summary directories
    summary_writer = tf.summary.FileWriter(log_dir)
    summary_writer.add_graph(sess.graph)
    # And then after everything is built, start the training loop.
    duration = 0.0
    best_val_neg_ll = np.finfo(float).max
    num_iter = config_train['num_iter']
    for step in xrange(init_step, num_iter):

        if ((step +1) % 50 == 0 and step < 1000) or ((step+1) % 250 == 0):
            input_image, compressed_image, reconstructed_image = sess.run([input_test, post_samples, sigmoid_output])
            path_dir = r'./images/' + sampler_name + '/'
            if not os.path.exists(path_dir):
                os.makedirs(path_dir)
            path = path_dir + 'input_image_%i.npy' % step
            input_image = np.asarray(input_image)
            save(path, input_image)
            path = path_dir +'compressed_image_%i.npy' % step
            compressed_image = np.asarray(compressed_image)
            save(path, compressed_image)
            path = path_dir +'reconstructed_image_%i.npy' % step
            reconstructed_image = np.asarray(reconstructed_image)
            save(path, reconstructed_image)

        start_time = time.time()
        # perform one iteration of training.
        # feed_dict = fill_feed_dict(data_sets.train, input_placeholder, batch_size)
        _, neg_elbo_value = sess.run([train_op, neg_elbo])
        duration += time.time() - start_time

        # Save a checkpoint and evaluate the model periodically.
        eval_iter = 20000 if num_iter > 1e5 else 10000
        if (step + 1) % eval_iter == 0 or (step + 1) == num_iter:
            print 'Skipping'
            # # if vae has rbm in its prior we should update its log Z.
            # if vae.should_compute_log_z():
            #     vae.prior.estimate_log_z(sess)
            #
            # # validate on the validation and test set
            # val_neg_elbo_value, val_neg_ll_value = evaluate(sess, neg_elbo_eval, log_iw_eval,
            #                                                 validation_data, datasets.validation, batch_size=eval_batch_size, k_iw=100)
            # test_neg_elbo_value, test_neg_ll_value = evaluate(sess, neg_elbo_eval, log_iw_eval,
            #                                                   test_data, datasets.test, batch_size=eval_batch_size, k_iw=100)
            # summary_str = sess.run(
            #     eval_summary_op, feed_dict={val_neg_elbo: val_neg_elbo_value, test_neg_elbo: test_neg_elbo_value,
            #                                 val_neg_ll: val_neg_ll_value, test_neg_ll: test_neg_ll_value})
            # summary_writer.add_summary(summary_str, step)
            #
            # Print('Step %d: val ELBO = %.2f test ELBO = %.2f, val NLL = %.2f, test NLL = %.2f' %
            #       (step, val_neg_elbo_value, test_neg_elbo_value, val_neg_ll_value, test_neg_ll_value))
            # # save model if it is better on validation set:
            # if val_neg_ll_value < best_val_neg_ll:
            #     best_val_neg_ll = val_neg_ll_value
            #     saver.save(sess, log_dir + '/', global_step=step)

        # Write the summaries and print an overview fairly often.

        ### Compression here
        report_iter = 1000
        if step % report_iter == 0 and step > 500:
            # print status to stdout.
            Print('Step %d, %.3f sec per step' % (step, duration/report_iter))
            duration = 0.0
            # Update the events file.
            summary_str = sess.run(train_summary_op)
            summary_writer.add_summary(summary_str, step)

        # in the last iteration, we load the best model based on the validation performance, and evaluate it on test
        if (step + 1) == num_iter:
            # Print('Final evaluation using the best saved model')
            # # reload the best model this is good when a model overfits.
            # ckpt = tf.train.get_checkpoint_state(log_dir)
            # saver.restore(sess, ckpt.model_checkpoint_path)
            # Print('Done restoring the model at step: %d' % sess.run(get_global_step_var()))
            # if vae.should_compute_log_z():
            #     vae.prior.estimate_log_z(sess)
            #
            # val_neg_elbo_value, val_neg_ll_value = evaluate(sess, neg_elbo_eval, log_iw_eval, input_val,
            #                                                 datasets.validation, eval_batch_size, k_iw=100)
            # test_neg_elbo_value, test_neg_ll_value = evaluate(sess, neg_elbo_eval, log_iw_eval, input_test,
            #                                                   datasets.test, eval_batch_size, k_iw=config_train['k_iw'])
            # summary_str = sess.run(
            #     eval_summary_op, feed_dict={val_neg_elbo: val_neg_elbo_value, test_neg_elbo: test_neg_elbo_value,
            #                                 val_neg_ll: val_neg_ll_value, test_neg_ll: test_neg_ll_value})
            # Print('Step %d: val ELBO = %.2f test ELBO = %.2f, val NLL = %.2f, test NLL = %.2f' %
            #       (step, val_neg_elbo_value, test_neg_elbo_value, val_neg_ll_value, test_neg_ll_value))
            # summary_writer.add_summary(summary_str, step+1)
            # summary_writer.flush()

            ### Then we calculate the psnr and ssim
            for i in range(10):
                test_data.batch_size = 100
                input_test = tf.cast(tf.greater(test_data, datasets.binarize_threshold), tf.float32)
                samples_in, samples_out = sess.run([input_test, sigmoid_output])
                path = path_dir + 'samples_in_%i.npy' % i
                save(path, samples_out)
                path = path_dir + 'samples_out_%i.npy' % i
                save(path, samples_in)
            test_data.batch_size = batch_size

            sess.close()
            tf.reset_default_graph()
            return


