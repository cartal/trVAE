import logging
import os

import keras
import numpy as np
import tensorflow as tf
from keras import backend as K
from keras.callbacks import CSVLogger, History, EarlyStopping
from keras.layers import Dense, BatchNormalization, Dropout, Input, Lambda, Activation
from keras.layers.advanced_activations import LeakyReLU
from keras.models import Model, load_model
from scipy import sparse

from rcvae.models.utils import label_encoder, shuffle_data

log = logging.getLogger(__file__)


class RCVAE:
    """
        Regularized C-VAE vector Network class. This class contains the implementation of Conditional
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

    def __init__(self, x_dimension, z_dimension=100, **kwargs):
        self.x_dim = x_dimension
        self.z_dim = z_dimension
        self.mmd_dim = kwargs.get('mmd_dimension', 128)

        self.lr = kwargs.get("learning_rate", 0.001)
        self.beta = kwargs.get("beta", 100)
        self.conditions = kwargs.get("condition_list")
        self.dr_rate = kwargs.get("dropout_rate", 0.2)
        self.model_to_use = kwargs.get("model_path", "./")
        self.kernel_method = kwargs.get("kernel", "multi-scale-rbf")

        self.x = Input(shape=(self.x_dim,), name="data")
        self.z = Input(shape=(self.z_dim,), name="latent_data")

        self.init_w = keras.initializers.glorot_normal()
        self._create_network()
        self._loss_function()

        self.encoder_model.summary()
        self.decoder_model.summary()
        self.rae_model.summary()

    def _encoder(self, x, name="encoder"):
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
        h = Dense(700, kernel_initializer=self.init_w, use_bias=False)(x)
        h = BatchNormalization()(h)
        h = LeakyReLU()(h)
        h = Dropout(self.dr_rate)(h)
        h = Dense(400, kernel_initializer=self.init_w, use_bias=False)(h)
        h = BatchNormalization()(h)
        h = LeakyReLU()(h)
        h = Dropout(self.dr_rate)(h)
        z = Dense(self.z_dim, kernel_initializer=self.init_w, name='mmd')(h)
        model = Model(inputs=x, outputs=z, name=name)
        return model

    def _decoder(self, z, name="decoder"):
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
        h = Dense(self.mmd_dim, kernel_initializer=self.init_w, use_bias=False)(z)
        h = BatchNormalization()(h)
        h = LeakyReLU()(h)
        h = Dense(400, kernel_initializer=self.init_w, use_bias=False)(h)
        h = BatchNormalization()(h)
        h = LeakyReLU()(h)
        h = Dense(700, kernel_initializer=self.init_w, use_bias=False)(h)
        h = BatchNormalization(axis=1)(h)
        h = LeakyReLU()(h)
        h = Dropout(self.dr_rate)(h)
        h = Dense(self.x_dim, kernel_initializer=self.init_w, use_bias=True)(h)
        h = Activation('relu', name="reconstruction_output")(h)
        model = Model(inputs=z, outputs=h, name=name)
        return model

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

        self.encoder_model = self._encoder(self.x, name="encoder")
        self.decoder_model = self._decoder(self.z, name="decoder")

        decoder_outputs = self.decoder_model(self.encoder_model(self.x))
        encoder_outputs = self.encoder_model(self.x)

        reconstruction_output = Lambda(lambda x: x, name="kl_reconstruction")(decoder_outputs[0])
        mmd_output = Lambda(lambda x: x, name="mmd")(encoder_outputs[0])
        self.rae_model = Model(inputs=self.x,
                               outputs=[reconstruction_output, mmd_output],
                               name="rae")

    @staticmethod
    def compute_kernel(x, y, kernel='rbf', **kwargs):
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
        scales = kwargs.get("scales", [])
        if kernel == "rbf":
            x_size = K.shape(x)[0]
            y_size = K.shape(y)[0]
            dim = K.shape(x)[1]
            tiled_x = K.tile(K.reshape(x, K.stack([x_size, 1, dim])), K.stack([1, y_size, 1]))
            tiled_y = K.tile(K.reshape(y, K.stack([1, y_size, dim])), K.stack([x_size, 1, 1]))
            return K.exp(-K.mean(K.square(tiled_x - tiled_y), axis=2) / K.cast(dim, tf.float32))
        elif kernel == 'raphy':
            scales = K.variable(value=np.asarray(scales))
            squared_dist = K.expand_dims(RCVAE.squared_distance(x, y), 0)
            scales = K.expand_dims(K.expand_dims(scales, -1), -1)
            weights = K.eval(K.shape(scales)[0])
            weights = K.variable(value=np.asarray(weights))
            weights = K.expand_dims(K.expand_dims(weights, -1), -1)
            return K.sum(weights * K.exp(-squared_dist / (K.pow(scales, 2))), 0)
        elif kernel == "multi-scale-rbf":
            sigmas = [1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 1e-1, 1, 5, 10, 15, 20, 25, 30, 35, 100, 1e3, 1e4, 1e5, 1e6]

            beta = 1. / (2. * (K.expand_dims(sigmas, 1)))
            distances = RCVAE.squared_distance(x, y)
            s = K.dot(beta, K.reshape(distances, (1, -1)))

            return K.reshape(tf.reduce_sum(tf.exp(-s), 0), K.shape(distances)) / len(sigmas)

    @staticmethod
    def squared_distance(x, y):  # returns the pairwise euclidean distance
        r = K.expand_dims(x, axis=1)
        return K.sum(K.square(r - y), axis=-1)

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
        x_kernel = RCVAE.compute_kernel(x, x, kernel=kernel, **kwargs)
        y_kernel = RCVAE.compute_kernel(y, y, kernel=kernel, **kwargs)
        xy_kernel = RCVAE.compute_kernel(x, y, kernel=kernel, **kwargs)
        return K.mean(x_kernel) + K.mean(y_kernel) - 2 * K.mean(xy_kernel)

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

        def batch_loss():
            def reconstruction_loss(y_true, y_pred):
                recon_loss = K.sum(K.square((y_true - y_pred)), axis=1)
                return recon_loss

            def mmd_loss(real_labels, y_pred):
                with tf.variable_scope("mmd_loss", reuse=tf.AUTO_REUSE):
                    real_labels = K.reshape(K.cast(real_labels, 'int32'), (-1,))
                    source_mmd, dest_mmd = tf.dynamic_partition(y_pred, real_labels, num_partitions=2)
                    loss = self.compute_mmd(source_mmd, dest_mmd, self.kernel_method)
                    return self.beta * loss

            self.cvae_optimizer = keras.optimizers.Adam(lr=self.lr)
            self.rae_model.compile(optimizer=self.cvae_optimizer,
                                   loss=[reconstruction_loss, mmd_loss],
                                   metrics={self.rae_model.outputs[0].name: reconstruction_loss,
                                            self.rae_model.outputs[1].name: mmd_loss})

        batch_loss()

    def to_latent(self, data):
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
        latent = self.encoder_model.predict(data)
        return latent

    def _reconstruct(self, data, use_data=False):
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
            latent = self.to_latent(data)
        rec_data = self.decoder_model.predict(latent)
        return rec_data

    def predict(self, data, data_space='None'):
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
            if data_space == 'latent':
                stim_pred = self._reconstruct(data.X.A, use_data=True)
            else:
                stim_pred = self._reconstruct(data.X.A)
        else:
            if data_space == 'latent':
                stim_pred = self._reconstruct(data.X, use_data=True)
            else:
                stim_pred = self._reconstruct(data.X)
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
        self.rae_model = load_model(os.path.join(self.model_to_use, 'mmd_ae.h5'), compile=False)
        self.encoder_model = load_model(os.path.join(self.model_to_use, 'encoder.h5'), compile=False)
        self.decoder_model = load_model(os.path.join(self.model_to_use, 'decoder.h5'), compile=False)
        self._loss_function()

    def train(self, train_data, use_validation=False, valid_data=None, n_epochs=25, batch_size=32, early_stop_limit=20,
              threshold=0.0025, initial_run=True,
              shuffle=True, verbose=2, save=True):
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

        if use_validation and valid_data is None:
            raise Exception("valid_data is None but use_validation is True.")

        callbacks = [
            History(),
            EarlyStopping(patience=early_stop_limit, monitor='val_loss', min_delta=threshold),
            CSVLogger(filename="./csv_logger.log")
        ]

        if sparse.issparse(train_data.X):
            train_data.X = train_data.X.A

        if shuffle:
            train_data = shuffle_data(train_data)

        x = train_data.X
        y = train_data.X
        if use_validation:
            if sparse.issparse(valid_data.X):
                valid_data.X = valid_data.X.A

            if shuffle:
                valid_data = shuffle_data(valid_data)

            x_valid = valid_data.X
            y_valid = valid_data.X
            histories = self.rae_model.fit(
                x=x,
                y=y,
                epochs=n_epochs,
                batch_size=batch_size,
                validation_data=(x_valid, y_valid),
                shuffle=shuffle,
                callbacks=callbacks,
                verbose=verbose)
        else:
            histories = self.rae_model.fit(
                x=x,
                y=y,
                epochs=n_epochs,
                batch_size=batch_size,
                shuffle=shuffle,
                callbacks=callbacks,
                verbose=verbose)
        if save:
            os.makedirs(self.model_to_use, exist_ok=True)
            self.rae_model.save(os.path.join(self.model_to_use, "mmd_rae.h5"), overwrite=True)
            self.encoder_model.save(os.path.join(self.model_to_use, "encoder.h5"), overwrite=True)
            self.decoder_model.save(os.path.join(self.model_to_use, "decoder.h5"), overwrite=True)
            log.info(f"Model saved in file: {self.model_to_use}. Training finished")
        return histories