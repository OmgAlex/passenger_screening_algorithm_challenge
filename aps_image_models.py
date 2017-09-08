from caching import cached, read_input_dir
from keras import backend as K

import numpy as np
import dataio
import aps_body_zone_models
import keras
import tqdm
import os
import pickle
import random
import h5py
import time


@cached(aps_body_zone_models.get_naive_partitioned_body_part_train_data, version=2)
def get_resnet50_cnn_codes(mode):
    if not os.path.exists('done'):
        model = keras.applications.ResNet50(include_top=False, input_shape=(256, 256, 3),
                                            pooling='avg')
        x, y = aps_body_zone_models.get_naive_partitioned_body_part_train_data(mode)

        codes = []
        for i in tqdm.tqdm(range(0, len(x), 32)):
            inputs = np.repeat(x[i:i+32, :, :, np.newaxis], 3, axis=3)
            inputs = keras.applications.resnet50.preprocess_input(inputs)
            codes.append(model.predict(inputs).reshape(len(inputs), -1))
        codes = np.concatenate(codes)

        np.save('x.npy', codes)
        np.save('y.npy', y)
        open('done', 'w').close()
    else:
        codes, y = np.load('x.npy'), np.load('y.npy')

    return codes, y


def _simple_model(init_filters, depth, learning_rate, image_size):
    model = keras.models.Sequential()
    model.add(keras.layers.BatchNormalization(input_shape=(image_size, image_size, 1)))
    for i in range(depth):
        for _ in range(2):
            model.add(keras.layers.Conv2D(2**(init_filters + i), (3, 3), padding='same',
                                          activation='relu'))
        model.add(keras.layers.BatchNormalization())
    # model.add(keras.layers.Flatten())
    # model.add(keras.layers.Dropout(0.5))
    model.add(keras.layers.GlobalAveragePooling2D())
    model.add(keras.layers.Dense(1, activation='sigmoid'))
    optimizer = keras.optimizers.Adam(learning_rate)
    model.compile(optimizer, 'binary_crossentropy')
    return model



@cached(aps_body_zone_models.get_naive_partitioned_body_part_train_data, version=0)
def localized_2d_cnn_hyperparameter_search(mode):
    assert mode in ('train', 'sample_train')

    if not os.path.exists('done'):

        train = 'train' if mode == 'train' else 'sample_train'
        valid = 'valid' if mode == 'train' else 'sample_valid'

        x_train, y_train = aps_body_zone_models.get_naive_partitioned_body_part_train_data(train)
        x_valid, y_valid = aps_body_zone_models.get_naive_partitioned_body_part_train_data(valid)
        train_gen = get_oversampled_data_generator(x_train, y_train, 32, 0.5)
        valid_gen = get_oversampled_data_generator(x_valid, y_valid, 32, 0.5)

        best_loss = 1e9
        for i in tqdm.tqdm(range(250)):
            init_filters = np.random.randint(1, 5)
            depth = np.random.randint(1, 5)
            learning_rate = 10 ** np.random.uniform(-1, -6)
            model = _simple_model(init_filters, depth, learning_rate, 256)

            info = 'model %s %s %s' % (2**init_filters, depth, learning_rate)
            print('running %s...' % info)
            history = model.fit_generator(train_gen, steps_per_epoch=10000//32, epochs=1,
                                          verbose=True, validation_data=valid_gen,
                                          validation_steps=2000//32)
            if history.history['val_loss'][-1] > 1:
                continue
            history = model.fit_generator(train_gen, steps_per_epoch=10000//32, epochs=9,
                                          verbose=True, validation_data=valid_gen,
                                          validation_steps=2000//32)

            train_loss = np.min(history.history['loss'])
            valid_loss = np.min(history.history['val_loss'])
            with open('log.txt', 'a') as log:
                log.write('%s train loss = %s, valid loss = %s\n' % \
                            (info, train_loss, valid_loss))

            if valid_loss < best_loss:
                best_loss = valid_loss
                model.save('best_model.h5')

        open('done', 'w').close()
    else:
        best_model = keras.models.load_model('best_model.h5')
    return best_model


@cached(aps_body_zone_models.get_naive_partitioned_body_part_train_data, version=1)
def train_local_2d_cnn_model(mode):
    assert mode in ('train', 'sample_train')

    def augment_data_generator(generator):
        gen = keras.preprocessing.image.ImageDataGenerator(
            rotation_range=0,
            width_shift_range=0.1,
            height_shift_range=0.1,
            shear_range=0.1,
            zoom_range=0.1,
            fill_mode='constant',
            horizontal_flip=True,
            vertical_flip=True,
        )
        for x_in, y_in in generator:
            x_out, y_out = next(gen.flow(x_in, y_in, batch_size=len(x_in)))
            x_out += np.random.normal(scale=0.05, size=x_out.shape)
            x_out = np.maximum(x_out, 0)
            yield x_out[:, ::4, ::4, :], y_out

    if not os.path.exists('model.h5'):
        train = 'train' if mode == 'train' else 'sample_train'
        valid = 'valid' if mode == 'train' else 'sample_valid'

        batch_size = 32
        if mode == 'train':
            steps_per_epoch, epochs = 10000//batch_size, 300
        else:
            steps_per_epoch, epochs = 10, 3

        x_train, y_train = aps_body_zone_models.get_naive_partitioned_body_part_train_data(train)
        x_valid, y_valid = aps_body_zone_models.get_naive_partitioned_body_part_train_data(valid)
        train_gen = get_oversampled_data_generator(x_train, y_train, batch_size,
                                                   steps_per_epoch * epochs, 0.5)
        valid_gen = get_oversampled_data_generator(x_valid, y_valid, batch_size, 1)
        train_gen_aug = augment_data_generator(train_gen)
        valid_gen_aug = augment_data_generator(valid_gen)

        model = _simple_model(3, 4, 1e-3, 64)
        model.fit_generator(train_gen_aug, steps_per_epoch=steps_per_epoch, epochs=epochs,
                            verbose=True)

        valid_loss = model.evaluate_generator(valid_gen_aug, steps=3*steps_per_epoch)
        with open('performance.txt', 'w') as f:
            f.write(str(valid_loss))
        model.save('model.h5')
    else:
        model = keras.models.load_model('model.h5')

    def predict(x):
        repeat = 128
        def gen():
            yield np.repeat(x[np.newaxis, :, :, np.newaxis], repeat, axis=0), np.zeros(repeat)

        x_aug, _ = next(augment_data_generator(gen()))
        ret = model.predict(x_aug)
        return np.mean(ret)

    return predict


def get_oversampled_data_generator(x, y, batch_size, steps, proportion_true=None):
    true_indexes = np.where(y == 1)[0]
    false_indexes = np.where(y == 0)[0]
    real_true = np.mean(y)
    if not proportion_true:
        proportion_true = real_true

    i = 0
    while True:
        if i < steps//3:
            proportion = proportion_true
        elif steps//3 <= i < 2*steps//3:
            proportion = proportion_true - (proportion_true-real_true)*(i-steps//3)/(steps/3)
        else:
            proportion = real_true

        num_true = int((random.random() * 2 * proportion) * batch_size)
        true_choice = np.random.choice(true_indexes, num_true)
        false_choice = np.random.choice(false_indexes, batch_size-num_true)

        yield (np.concatenate([x[true_choice, :, :, np.newaxis],
                               x[false_choice, :, :, np.newaxis]]),
               np.concatenate([y[true_choice], y[false_choice]]))
        i += 1


@cached(aps_body_zone_models.get_naive_partitioned_body_part_test_data, train_local_2d_cnn_model,
        version=1)
def get_local_2d_cnn_test_predictions(mode):
    assert mode in ('test', 'sample_test')

    if not os.path.exists('ret.pickle'):
        predictor = train_local_2d_cnn_model('train' if mode == 'test' else 'sample_train')
        data = aps_body_zone_models.get_naive_partitioned_body_part_test_data(mode)
        ret = {}

        for label, images in tqdm.tqdm(data.items()):
            ret[label] = [None] * 17
            for i in range(17):
                ret[label][i] = predictor(images[i])

        with open('ret.pickle', 'wb') as f:
            pickle.dump(ret, f)
    else:
        with open('ret.pickle', 'rb') as f:
            ret = pickle.load(f)
    return ret


def _augment_data_generator(x, y, batch_size):
    gen = keras.preprocessing.image.ImageDataGenerator(
        width_shift_range=0.1,
        height_shift_range=0.1,
        shear_range=0.1,
        zoom_range=0.1,
        fill_mode='constant',
        horizontal_flip=True,
        vertical_flip=True,
    )
    seed = int(time.time())
    for i in range(0, len(x), batch_size):
        batch = x[i:i+batch_size].copy()
        for j in range(len(batch)):
            for k in range(x.shape[1]):
                batch[j, k, :, :, 0] /= np.max(batch[j, k, :, :, 0])
                for l in range(x.shape[4]):
                    np.random.seed(seed)
                    batch[j, k, :, :, l] = gen.random_transform(batch[j, k, :, :, l, np.newaxis])[:, :, 0]
                seed += 1
        batch[:, :, :, :, 0] += np.random.uniform(0, 0.1, batch.shape[:-1])
        yield batch, y[i:i+batch_size]


@cached(aps_body_zone_models.get_global_image_train_data)
def get_augmented_global_image_data(mode, size):
    if not os.path.exists('done'):
        x_in, y_in = aps_body_zone_models.get_global_image_train_data(mode, size)
        num_dsets = 5 if mode.startswith('sample') else 50
        f = h5py.File('data.hdf5', 'w')
        x = f.create_dataset('x', (num_dsets*len(x_in),) + x_in.shape[1:])
        y = f.create_dataset('y', (num_dsets*len(y_in),) + y_in.shape[1:])
        batch_size = 32

        for i in tqdm.tqdm(range(num_dsets)):
            for j, (xb, yb) in enumerate(_augment_data_generator(x_in, y_in, batch_size)):
                st = i*len(x_in) + j*batch_size
                x[st:st+len(xb)] = xb
                y[st:st+len(yb)] = yb

        open('done', 'w').close()
    else:
        f = h5py.File('data.hdf5', 'r')
        x, y = f['x'], f['y']
    return x, y


def _global_model(nfilters, nconv, nlayers, learning_rate, size, default_pred, fc_per_zone):
    K.set_learning_phase(1)
    bias = np.log(default_pred / (1 - default_pred))

    class SplitDenseLayer(keras.engine.topology.Layer):
        def build(self, input_shape):
            self.kernel = self.add_weight(name='kernel', shape=(1,)+input_shape[1:],
                                          initializer=keras.initializers.Zeros(),
                                          trainable=True)
            self.bias = self.add_weight(name='bias', shape=(1, input_shape[1]),
                                        initializer=keras.initializers.Constant(bias),
                                        trainable=True)
            super(SplitDenseLayer, self).build(input_shape)
        
        def call(self, x):
            return K.sum(x * self.kernel, axis=-1) + self.bias
        
        def compute_output_shape(self, input_shape):
            return input_shape[:-1]

    cnn_inputs = keras.layers.Input(shape=(size, size, 18))
    cnn = keras.layers.Lambda(lambda x: x[..., 0:1])(cnn_inputs)
    for _ in range(nlayers):
        for _ in range(nconv):
            cnn = keras.layers.BatchNormalization()(cnn)
            cnn = keras.layers.Conv2D(nfilters, (3, 3), padding='same', activation='relu')(cnn)
        cnn = keras.layers.MaxPool2D((2, 2), (1, 1), padding='same')(cnn)
    cnn = keras.layers.core.Reshape((size * size, -1))(cnn)
    cnn = keras.layers.core.Permute((2, 1))(cnn)
    body_zones = keras.layers.Lambda(lambda x: x[..., 1:])(cnn_inputs)
    body_zones = keras.layers.core.Reshape((size * size, -1))(body_zones)
    zone_features = keras.layers.core.Lambda(lambda x: K.batch_dot(x[0], x[1]))([cnn, body_zones])
    image_model = keras.models.Model(cnn_inputs, zone_features)

    inputs = keras.layers.Input(shape=(4, size, size, 18))
    all_zone_features = keras.layers.wrappers.TimeDistributed(image_model)(inputs)
    all_zone_features = keras.layers.core.Reshape((-1, 17))(all_zone_features)
    all_zone_features = keras.layers.core.Permute((2, 1))(all_zone_features)

    if fc_per_zone:
        predictions = SplitDenseLayer()(all_zone_features)
        predictions = keras.layers.Activation(keras.activations.sigmoid)(predictions)
    else:
        predictions = keras.layers.wrappers.TimeDistributed(
            keras.layers.Dense(1, activation='sigmoid',
                               kernel_initializer=keras.initializers.Constant(0),
                               bias_initializer=keras.initializers.Constant(bias)))(all_zone_features)
        predictions = keras.layers.Reshape((-1,))(predictions)


    model = keras.models.Model(inputs=inputs, outputs=predictions)

    optimizer = keras.optimizers.Adam(learning_rate)
    model.compile(optimizer=optimizer, loss='binary_crossentropy')
    return model


@cached(get_augmented_global_image_data, version=2)
def train_global_2d_cnn_model(mode, per_zone_fc):
    assert mode in ('train', 'sample_train')

    if not os.path.exists('model.h5'):
        train = 'train' if mode == 'train' else 'sample_train'
        valid = 'valid' if mode == 'train' else 'sample_valid'

        batch_size = 32
        image_size = 128
        repeat_batch = 5
        if mode == 'train':
            epochs = 1
        else:
            epochs = 1

        x_train, y_train = get_augmented_global_image_data(train, image_size)
        x_valid, y_valid = get_augmented_global_image_data(valid, image_size)
        y_train, y_valid = y_train[()], y_valid[()]
        model = _global_model(32, 3, 3, 1e-4, 128, np.mean(y_train), per_zone_fc)

        for epoch in tqdm.trange(epochs, desc='epochs'):
            batchsize = int(10e9) // x_train[0, ...].nbytes
            for i in tqdm.trange(0, len(x_train), batchsize, desc='batches'):
                x_batch, y_batch = x_train[i:i+batchsize], y_train[i:i+batchsize]
                for batch in range(repeat_batch):
                    model.fit(x_batch, y_batch, batch_size=batch_size)

        valid_loss = model.evaluate(x_valid, y_valid)
        with open('performance.txt', 'w') as f:
            f.write(str(valid_loss))
        model.save('model.h5')
    else:
        model = keras.models.load_model('model.h5')

    return model



@cached(get_local_2d_cnn_test_predictions, version=0)
def write_local_2d_cnn_test_predictions(mode):
    preds = get_local_2d_cnn_test_predictions(mode)
    dataio.write_answer_csv(preds)