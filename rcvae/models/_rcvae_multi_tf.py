import logging
import os

import tensorflow as tf
from keras.utils import to_categorical
from scipy import sparse

from rcvae.models.utils import shuffle_data
from rcvae.models.utils import label_encoder

log = logging.getLogger(__file__)


class RCVAEMultiTF:
    """
        C-VAE vector Network class. This class contains the implementation of Conditional
        Variational Auto-encoder network.

        # Parameters
            kwargs:
                key: `dropout_rate`: float
                        dropout rate
                key: `learning_rate`: float
                    learning rate of optimization algorithm
                key: `model_path`: basestring
                    path to save the model after training
                key: `alpha`: float
                    alpha coefficient for loss.
                key: `beta`: float
                    beta coefficient for loss.
            x_dimension: integer
                number of gene expression space dimensions.
            z_dimension: integer
                number of latent space dimensions.
    """

    def __init__(self, x_dimension, z_dimension=100, n_conditions=2, **kwargs):
        tf.reset_default_graph()
        self.x_dim = x_dimension
        self.z_dim = z_dimension
        self.mmd_dim = kwargs.get('mmd_dimension', 128)
        self.n_conditions = n_conditions

        self.lr = kwargs.get("learning_rate", 0.001)
        self.alpha = kwargs.get("alpha", 0.001)
        self.beta = kwargs.get("beta", 100)
        self.dr_rate = kwargs.get("dropout_rate", 0.2)
        self.model_to_use = kwargs.get("model_path", "./")
        self.kernel_method = kwargs.get("kernel", "multi-scale-rbf")
        self.output_activation = kwargs.get("output_activation", 'relu')
        self.loss_fn = kwargs.get("loss_fn", 'mse')
        self.ridge = kwargs.get('ridge', 0.1)
        self.clip_value = kwargs.get('clip_value', 3.0)

        self.is_training = tf.placeholder(tf.bool, name='training_flag')
        self.global_step = tf.Variable(0, name='global_step', trainable=False, dtype=tf.int32)
        self.x = tf.placeholder(tf.float32, shape=[None, self.x_dim], name="data")
        self.z = tf.placeholder(tf.float32, shape=[None, self.z_dim], name="latent")
        self.encoder_labels = tf.placeholder(tf.float32, shape=[None, self.n_conditions], name="labels")
        self.decoder_labels = tf.placeholder(tf.float32, shape=[None, self.n_conditions], name="labels")
        self.time_step = tf.placeholder(tf.int32)
        self.size = tf.placeholder(tf.int32)
        self.init_w = tf.contrib.layers.xavier_initializer()

        self._create_network()
        self._loss_function()
        init = tf.global_variables_initializer()
        self.sess = tf.InteractiveSession()
        self.saver = tf.train.Saver(max_to_keep=1)
        self.sess.run(init)

    def _encoder(self):
        """
            Constructs the encoder sub-network of C-VAE. This function implements the
            encoder part of Variational Auto-encoder. It will transform primary
            data in the `n_vars` dimension-space to the `z_dimension` latent space.

            # Parameters
                No parameters are needed.

            # Returns
                mean: Tensor
                    A dense layer consists of means of gaussian distributions of latent space dimensions.
                log_var: Tensor
                    A dense layer consists of log transformed variances of gaussian distributions of latent space dimensions.
        """
        with tf.variable_scope("encoder", reuse=tf.AUTO_REUSE):
            xy = tf.concat([self.x, self.encoder_labels], axis=1)
            h = tf.layers.dense(inputs=xy, units=800, kernel_initializer=self.init_w, use_bias=False)
            h = tf.layers.batch_normalization(h, axis=1, training=self.is_training)
            h = tf.nn.leaky_relu(h)
            h = tf.layers.dense(inputs=h, units=800, kernel_initializer=self.init_w, use_bias=False)
            h = tf.layers.batch_normalization(h, axis=1, training=self.is_training)
            h = tf.nn.leaky_relu(h)
            h = tf.layers.dropout(h, self.dr_rate, training=self.is_training)
            h = tf.layers.dense(inputs=h, units=self.mmd_dim, kernel_initializer=self.init_w, use_bias=False)
            h = tf.layers.batch_normalization(h, axis=1, training=self.is_training)
            h = tf.nn.leaky_relu(h)
            h = tf.layers.dropout(h, self.dr_rate, training=self.is_training)
            mean = tf.layers.dense(inputs=h, units=self.z_dim, kernel_initializer=self.init_w)
            log_var = tf.layers.dense(inputs=h, units=self.z_dim, kernel_initializer=self.init_w)
            return mean, log_var

    def _mmd_decoder(self):
        """
            Constructs the decoder sub-network of C-VAE. This function implements the
            decoder part of Variational Auto-encoder. It will transform constructed
            latent space to the previous space of data with n_dimensions = n_vars.

            # Parameters
                No parameters are needed.

            # Returns
                h: Tensor
                    A Tensor for last dense layer with the shape of [n_vars, ] to reconstruct data.

        """
        with tf.variable_scope("decoder", reuse=tf.AUTO_REUSE):
            xy = tf.concat([self.z_mean, self.decoder_labels], axis=1)
            h = tf.layers.dense(inputs=xy, units=self.mmd_dim, kernel_initializer=self.init_w, use_bias=False)
            h = tf.layers.batch_normalization(h, axis=1, training=self.is_training)
            h_mmd = tf.nn.leaky_relu(h)
            h = tf.layers.dropout(h_mmd, self.dr_rate, training=self.is_training)
            h = tf.layers.dense(inputs=h, units=800, kernel_initializer=self.init_w, use_bias=False)
            h = tf.layers.batch_normalization(h, axis=1, training=self.is_training)
            h = tf.nn.leaky_relu(h)
            h = tf.layers.dropout(h, self.dr_rate, training=self.is_training)
            h = tf.layers.dense(inputs=h, units=800, kernel_initializer=self.init_w, use_bias=False)
            h = tf.layers.batch_normalization(h, axis=1, training=self.is_training)
            h = tf.nn.leaky_relu(h)
            h = tf.layers.dropout(h, self.dr_rate, training=self.is_training)
            h = tf.layers.dense(inputs=h, units=self.x_dim, kernel_initializer=self.init_w, use_bias=True)
            if self.output_activation == 'relu':
                h = tf.nn.relu(h)
            return h, h_mmd

    def _sample_z(self):
        """
            Samples from standard Normal distribution with shape [size, z_dim] and
            applies re-parametrization trick. It is actually sampling from latent
            space distributions with N(mu, var) computed in `_encoder` function.

            # Parameters
                No parameters are needed.

            # Returns
                The computed Tensor of samples with shape [size, z_dim].
        """
        eps = tf.random_normal(shape=[self.size, self.z_dim])
        return self.mu + tf.exp(self.log_var / 2) * eps

    def _create_network(self):
        """
            Constructs the whole C-VAE network. It is step-by-step constructing the C-VAE
            network. First, It will construct the encoder part and get mu, log_var of
            latent space. Second, It will sample from the latent space to feed the
            decoder part in next step. Finally, It will reconstruct the data by
            constructing decoder part of C-VAE.

            # Parameters
                No parameters are needed.

            # Returns
                Nothing will be returned.
        """
        self.mu, self.log_var = self._encoder()
        self.z_mean = self._sample_z()
        self.x_hat, self.mmd_hl = self._mmd_decoder()

    @staticmethod
    def compute_kernel(x, y, kernel, **kwargs):
        """
            Computes RBF kernel between x and y.

            # Parameters
                x: Tensor
                    Tensor with shape [batch_size, z_dim]
                y: Tensor
                    Tensor with shape [batch_size, z_dim]

            # Returns
                returns the computed RBF kernel between x and y
        """
        if kernel == "rbf":
            x_size = tf.shape(x)[0]
            y_size = tf.shape(y)[0]
            dim = tf.shape(x)[1]
            tiled_x = tf.tile(tf.reshape(x, tf.stack([x_size, 1, dim])), tf.stack([1, y_size, 1]))
            tiled_y = tf.tile(tf.reshape(y, tf.stack([1, y_size, dim])), tf.stack([x_size, 1, 1]))
            return tf.exp(-tf.reduce_mean(tf.square(tiled_x - tiled_y), axis=2) / tf.cast(dim, tf.float32))
        elif kernel == "multi-scale-rbf":
            sigmas = [1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 1e-1, 1, 5, 10, 15, 20, 25, 30, 35, 100, 1e3, 1e4, 1e5, 1e6]

            beta = 1. / (2. * (tf.expand_dims(sigmas, 1)))
            distances = RCVAEMultiTF.squared_distance(x, y)
            s = tf.matmul(beta, tf.reshape(distances, (1, -1)))

            return tf.reshape(tf.reduce_sum(tf.exp(-s), 0), tf.shape(distances)) / len(sigmas)

    @staticmethod
    def squared_distance(x, y):  # returns the pairwise euclidean distance
        r = tf.expand_dims(x, axis=1)
        return tf.reduce_sum(tf.square(r - y), axis=-1)

    @staticmethod
    def compute_mmd(x, y, kernel, **kwargs):  # [batch_size, z_dim] [batch_size, z_dim]
        """
            Computes Maximum Mean Discrepancy(MMD) between x and y.

            # Parameters
                x: Tensor
                    Tensor with shape [batch_size, z_dim]
                y: Tensor
                    Tensor with shape [batch_size, z_dim]

            # Returns
                returns the computed MMD between x and y
        """
        x_kernel = RCVAEMultiTF.compute_kernel(x, x, kernel=kernel, **kwargs)
        y_kernel = RCVAEMultiTF.compute_kernel(y, y, kernel=kernel, **kwargs)
        xy_kernel = RCVAEMultiTF.compute_kernel(x, y, kernel=kernel, **kwargs)
        return tf.reduce_mean(x_kernel) + tf.reduce_mean(y_kernel) - 2 * tf.reduce_mean(
            xy_kernel)

    def _loss_function(self):
        """
            Defines the loss function of C-VAE network after constructing the whole
            network. This will define the KL Divergence and Reconstruction loss for
            C-VAE and also defines the Optimization algorithm for network. The C-VAE Loss
            will be weighted sum of reconstruction loss and KL Divergence loss.

            # Parameters
                No parameters are needed.

            # Returns
                Nothing will be returned.
        """
        self.kl_loss = 0.5 * tf.reduce_sum(
            tf.exp(self.log_var) + tf.square(self.mu) - 1. - self.log_var, 1)
        self.recon_loss = 0.5 * tf.reduce_sum(tf.square((self.x - self.x_hat)), 1)
        self.kl_recon_loss = tf.reduce_mean(self.recon_loss + self.alpha * self.kl_loss)

        # MMD loss computation
        with tf.variable_scope("mmd_loss", reuse=tf.AUTO_REUSE):
            real_labels = tf.argmax(self.decoder_labels, axis=1)
            real_labels = tf.reshape(tf.cast(real_labels, 'int32'), (-1,))
            conditions_mmd = tf.dynamic_partition(self.mmd_hl, real_labels, num_partitions=self.n_conditions)
            loss = 0.0
            for i in range(len(conditions_mmd)):
                for j in range(i):
                    loss += self.compute_mmd(conditions_mmd[j], conditions_mmd[j + 1], self.kernel_method)
            self.mmd_loss = self.beta * loss

        self.trvae_loss = self.mmd_loss + self.kl_recon_loss
        with tf.control_dependencies(tf.get_collection(tf.GraphKeys.UPDATE_OPS)):
            self.solver = tf.train.AdamOptimizer(learning_rate=self.lr).minimize(self.trvae_loss)

    def to_latent(self, data, labels):
        """
            Map `data` in to the latent space. This function will feed data
            in encoder part of C-VAE and compute the latent space coordinates
            for each sample in data.

            # Parameters
                data: `~anndata.AnnData`
                    Annotated data matrix to be mapped to latent space. `data.X` has to be in shape [n_obs, n_vars].
                labels: numpy nd-array
                    `numpy nd-array` of labels to be fed as CVAE's condition array.

            # Returns
                latent: numpy nd-array
                    returns array containing latent space encoding of 'data'
        """
        if sparse.issparse(data):
            data = data.A
        latent = self.sess.run(self.z_mean, feed_dict={self.x: data, self.encoder_labels: labels,
                                                       self.size: data.shape[0], self.is_training: False})
        return latent

    def to_mmd_layer(self, data, labels):
        """
                    Map `data` in to the pn layer after latent layer. This function will feed data
                    in encoder part of C-VAE and compute the latent space coordinates
                    for each sample in data.

                    # Parameters
                        data: `~anndata.AnnData`
                            Annotated data matrix to be mapped to latent space. `data.X` has to be in shape [n_obs, n_vars].
                        labels: numpy nd-array
                            `numpy nd-array` of labels to be fed as CVAE's condition array.

                    # Returns
                        latent: numpy nd-array
                            returns array containing latent space encoding of 'data'
                """

        latent = self.sess.run(self.mmd_hl, feed_dict={self.x: data, self.decoder_labels: labels,
                                                       self.size: data.shape[0], self.is_training: False})
        return latent

    def _reconstruct(self, data, labels, use_data=False):
        """
            Map back the latent space encoding via the decoder.

            # Parameters
                data: `~anndata.AnnData`
                    Annotated data matrix whether in latent space or primary space.

                labels: numpy nd-array
                    `numpy nd-array` of labels to be fed as CVAE's condition array.

                use_data: bool
                    this flag determines whether the `data` is already in latent space or not.
                    if `True`: The `data` is in latent space (`data.X` is in shape [n_obs, z_dim]).
                    if `False`: The `data` is not in latent space (`data.X` is in shape [n_obs, n_vars]).

            # Returns
                rec_data: 'numpy nd-array'
                    returns 'numpy nd-array` containing reconstructed 'data' in shape [n_obs, n_vars].
        """
        if use_data:
            latent = data
        else:
            latent = self.to_latent(data, labels)
        rec_data = self.sess.run(self.x_hat, feed_dict={self.z_mean: latent, self.encoder_labels: labels.reshape(-1, 1),
                                                        self.is_training: False})
        return rec_data

    def predict(self, data, labels):
        """
            Predicts the cell type provided by the user in stimulated condition.

            # Parameters
                data: `~anndata.AnnData`
                    Annotated data matrix whether in primary space.

                labels: numpy nd-array
                    `numpy nd-array` of labels to be fed as CVAE's condition array.

            # Returns
                stim_pred: numpy nd-array
                    `numpy nd-array` of predicted cells in primary space.

            # Example
            ```python
            import scanpy as sc
            import scgen
            train_data = sc.read("train_kang.h5ad")
            validation_data = sc.read("./data/validation.h5ad")
            network = scgen.CVAE(train_data=train_data, use_validation=True, validation_data=validation_data, model_path="./saved_models/", conditions={"ctrl": "control", "stim": "stimulated"})
            network.train(n_epochs=20)
            prediction = network.predict('CD4T', obs_key={"cell_type": ["CD8T", "NK"]})
            ```
        """
        if sparse.issparse(data.X):
            data.X = data.X.A

        stim_pred = self._reconstruct(data.X, labels)

        return stim_pred

    def restore_model(self):
        """
            restores model weights from `model_to_use`.

            # Parameters
                No parameters are needed.

            # Returns
                Nothing will be returned.

            # Example
            ```python
            import scanpy as sc
            import scgen
            train_data = sc.read("./data/train_kang.h5ad")
            validation_data = sc.read("./data/valiation.h5ad")
            network = scgen.CVAE(train_data=train_data, use_validation=True, validation_data=validation_data, model_path="./saved_models/", conditions={"ctrl": "control", "stim": "stimulated"})
            network.restore_model()
            ```
        """
        self.saver.restore(self.sess, self.model_to_use)

    def train(self, train_data, le=None, condition_key='condition', use_validation=False, valid_data=None, n_epochs=25,
              batch_size=32, early_stop_limit=20, lr_reducer=0,
              threshold=0.0025, initial_run=True, shuffle=True, verbose=True):
        """
            Trains the network `n_epochs` times with given `train_data`
            and validates the model using validation_data if it was given
            in the constructor function. This function is using `early stopping`
            technique to prevent overfitting.

            # Parameters
                n_epochs: int
                    number of epochs to iterate and optimize network weights
                early_stop_limit: int
                    number of consecutive epochs in which network loss is not going lower.
                    After this limit, the network will stop training.
                threshold: float
                    Threshold for difference between consecutive validation loss values
                    if the difference is upper than this `threshold`, this epoch will not
                    considered as an epoch in early stopping.
                full_training: bool
                    if `True`: Network will be trained with all batches of data in each epoch.
                    if `False`: Network will be trained with a random batch of data in each epoch.
                initial_run: bool
                    if `True`: The network will initiate training and log some useful initial messages.
                    if `False`: Network will resume the training using `restore_model` function in order
                        to restore last model which has been trained with some training dataset.


            # Returns
                Nothing will be returned

            # Example
            ```python
            import scanpy as sc
            import scgen
            train_data = sc.read(train_katrain_kang.h5ad           >>> validation_data = sc.read(valid_kang.h5ad)
            network = scgen.CVAE(train_data=train_data, use_validation=True, validation_data=validation_data, model_path="./saved_models/", conditions={"ctrl": "control", "stim": "stimulated"})
            network.train(n_epochs=20)
            ```
        """
        if initial_run:
            log.info("----Training----")
            assign_step_zero = tf.assign(self.global_step, 0)
            _init_step = self.sess.run(assign_step_zero)
        if not initial_run:
            self.saver.restore(self.sess, self.model_to_use)
        train_labels, le = label_encoder(train_data, label_encoder=le, condition_key=condition_key)
        if shuffle:
            train_data, train_labels = shuffle_data(train_data, train_labels)
        if use_validation and valid_data is None:
            raise Exception("valid_data is None but use_validation is True.")
        if use_validation:
            valid_labels, _ = label_encoder(valid_data, label_encoder=le, condition_key=condition_key)
        loss_hist = []
        patience = early_stop_limit
        min_delta = threshold
        patience_cnt = 0
        for it in range(n_epochs):
            increment_global_step_op = tf.assign(self.global_step, self.global_step + 1)
            _step = self.sess.run(increment_global_step_op)
            current_step = self.sess.run(self.global_step)
            train_loss = 0
            train_mmd_loss = 0
            for lower in range(0, train_data.shape[0], batch_size):
                upper = min(lower + batch_size, train_data.shape[0])
                if sparse.issparse(train_data.X):
                    x_mb = train_data[lower:upper, :].X.A
                else:
                    x_mb = train_data[lower:upper, :].X
                y_mb = train_labels[lower:upper]
                y_mb = to_categorical(y_mb, num_classes=self.n_conditions)
                _, current_loss_train, current_mmd_loss_train = self.sess.run(
                    [self.solver, self.trvae_loss, self.mmd_loss],
                    feed_dict={self.x: x_mb, self.encoder_labels: y_mb,
                               self.decoder_labels: y_mb,
                               self.time_step: current_step,
                               self.size: len(x_mb), self.is_training: True})
                train_loss += current_loss_train
                train_mmd_loss += current_mmd_loss_train
            if use_validation:
                valid_loss = 0
                valid_mmd_loss = 0
                for lower in range(0, valid_data.shape[0], batch_size):
                    upper = min(lower + batch_size, valid_data.shape[0])
                    if sparse.issparse(valid_data.X):
                        x_mb = valid_data[lower:upper, :].X.A
                    else:
                        x_mb = valid_data[lower:upper, :].X
                    y_mb = valid_labels[lower:upper]
                    y_mb = to_categorical(y_mb, num_classes=self.n_conditions)
                    current_loss_valid, current_mmd_loss_valid = self.sess.run([self.trvae_loss, self.mmd_loss],
                                                                               feed_dict={self.x: x_mb,
                                                                                          self.encoder_labels: y_mb,
                                                                                          self.decoder_labels: y_mb,
                                                                                          self.time_step: current_step,
                                                                                          self.size: len(x_mb),
                                                                                          self.is_training: False})
                    valid_loss += current_loss_valid
                    valid_mmd_loss += current_mmd_loss_valid
                loss_hist.append(valid_loss / valid_data.shape[0])
                if it > 0 and loss_hist[it - 1] - loss_hist[it] > min_delta:
                    patience_cnt = 0
                else:
                    patience_cnt += 1
                if patience_cnt > patience:
                    os.makedirs(self.model_to_use, exist_ok=True)
                    save_path = self.saver.save(self.sess, self.model_to_use)
                    break
                if verbose:
                    print(f"Epoch {it}: Train Loss: {train_loss / (train_data.shape[0] // batch_size):.4f}    Train MMD Loss: {train_mmd_loss / (train_data.shape[0] // batch_size):.4f}    Valid Loss: {valid_loss / (valid_data.shape[0] // batch_size):.4f}    Valid MMD Loss: {valid_mmd_loss / (valid_data.shape[0] // batch_size):.4f}")
            else:
                if verbose:
                    print(f"Epoch {it}: Train Loss: {train_loss / (train_data.shape[0] // batch_size):.4f}    Train MMD Loss: {train_mmd_loss / (train_data.shape[0] // batch_size)}")
        os.makedirs(self.model_to_use, exist_ok=True)
        save_path = self.saver.save(self.sess, self.model_to_use)
        print(f"Model saved in file: {save_path}. Training finished")