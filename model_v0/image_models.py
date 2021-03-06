from common.caching import cached, read_input_dir

from . import dataio
from . import body_zone_models

from keras import backend as K

import numpy as np
import keras
import tqdm
import os
import pickle
import random
import h5py
import time


@cached(body_zone_models.get_naive_partitioned_body_part_train_data, version=2)
def get_resnet50_cnn_codes(mode):
    if not os.path.exists('done'):
        model = keras.applications.ResNet50(include_top=False, input_shape=(256, 256, 3),
                                            pooling='avg')
        x, y = body_zone_models.get_naive_partitioned_body_part_train_data(mode)

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



@cached(body_zone_models.get_naive_partitioned_body_part_train_data, version=0)
def localized_2d_cnn_hyperparameter_search(mode):
    assert mode in ('train', 'sample_train')

    if not os.path.exists('done'):

        train = 'train' if mode == 'train' else 'sample_train'
        valid = 'valid' if mode == 'train' else 'sample_valid'

        x_train, y_train = body_zone_models.get_naive_partitioned_body_part_train_data(train)
        x_valid, y_valid = body_zone_models.get_naive_partitioned_body_part_train_data(valid)
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


@cached(body_zone_models.get_naive_partitioned_body_part_train_data, version=1)
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

        x_train, y_train = body_zone_models.get_naive_partitioned_body_part_train_data(train)
        x_valid, y_valid = body_zone_models.get_naive_partitioned_body_part_train_data(valid)
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


@cached(body_zone_models.get_naive_partitioned_body_part_test_data, train_local_2d_cnn_model,
        version=1)
def get_local_2d_cnn_test_predictions(mode):
    assert mode in ('test', 'sample_test')

    if not os.path.exists('ret.pickle'):
        predictor = train_local_2d_cnn_model('train' if mode == 'test' else 'sample_train')
        data = body_zone_models.get_naive_partitioned_body_part_test_data(mode)
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


def _augment_data_generator(x, y, batch_size, symmetric):
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
                batch[j, k, :, :, 0:1+symmetric] /= np.max(batch[j, k, :, :, 0:1+symmetric])
                for l in range(x.shape[4]):
                    np.random.seed(seed)
                    batch[j, k, :, :, l] = \
                        gen.random_transform(batch[j, k, :, :, l, np.newaxis])[:, :, 0]
                seed += 1
        batch[:, :, :, :, 0:1+symmetric] += np.random.uniform(0, 0.1,
                                                              batch.shape[:-1] + (1+symmetric,))
        yield batch, y[i:i+batch_size]


@cached(body_zone_models.get_global_image_train_data, version=2)
def get_augmented_global_image_train_data(mode, size, symmetric):
    if not os.path.exists('done'):
        x_in, y_in = body_zone_models.get_global_image_train_data(mode, size, symmetric)
        num_dsets = 5 if mode.startswith('sample') else 50
        f = h5py.File('data.hdf5', 'w')
        x = f.create_dataset('x', (num_dsets*len(x_in),) + x_in.shape[1:])
        batch_size = 32

        for i in tqdm.tqdm(range(num_dsets)):
            for j, (xb, yb) in enumerate(_augment_data_generator(x_in, y_in, batch_size,
                                                                 symmetric)):
                st = i*len(x_in) + j*batch_size
                x[st:st+len(xb)] = xb

        y = f.create_dataset('y', (num_dsets*len(y_in),) + y_in.shape[1:])
        y[()] = np.tile(y_in, (num_dsets, 1))

        open('done', 'w').close()
    else:
        f = h5py.File('data.hdf5', 'r')
        x, y = f['x'], f['y']
    return x, y


@cached(body_zone_models.get_global_image_test_data, version=1)
def get_augmented_global_image_test_data(mode, size):
    if not os.path.exists('done'):
        x_in, files = body_zone_models.get_global_image_test_data(mode, size)
        num_dsets = 5 if mode.startswith('sample') else 50
        f = h5py.File('data.hdf5', 'w')
        x = f.create_dataset('x', (num_dsets*len(x_in),) + x_in.shape[1:])
        batch_size = 32

        for i in tqdm.trange(num_dsets):
            for j, (xb, _) in enumerate(_augment_data_generator(x_in, np.zeros(batch_size),
                                                                batch_size, False)):
                st = i*len(x_in) + j*batch_size
                x[st:st+len(xb)] = xb

        with open('files.txt', 'w') as f2:
            f2.write('\n'.join(files))

        open('done', 'w').close()
    else:
        f = h5py.File('data.hdf5', 'r')
        x = f['x']
        with open('files.txt', 'r') as f2:
            files = f2.read().splitlines()
    return x, files


class SplitDenseLayer(keras.engine.topology.Layer):
    def __init__(self, default_pred, **kwargs):
        self.default_pred = default_pred
        super(SplitDenseLayer, self).__init__(**kwargs)

    def build(self, input_shape):
        self.kernel = self.add_weight(name='kernel', shape=(1,)+input_shape[1:],
                                      initializer=keras.initializers.Zeros(),
                                      trainable=True)
        default_bias = np.log(self.default_pred / (1 - self.default_pred))
        self.bias = self.add_weight(name='bias', shape=(1, input_shape[1]),
                                    initializer=keras.initializers.Constant(default_bias),
                                    trainable=True)
        super(SplitDenseLayer, self).build(input_shape)

    def call(self, x):
        return K.sum(x * self.kernel, axis=-1) + self.bias

    def compute_output_shape(self, input_shape):
        return input_shape[:-1]

    def get_config(self):
        return {'default_pred': self.default_pred}

    @classmethod
    def from_config(cls, config):
        return cls(config['default_pred'])


def _global_cnn(nfilters, nconv, nlayers, image_size, symmetric):
    cnn_inputs = keras.layers.Input(shape=(image_size, image_size, 18+symmetric))
    if symmetric:
        cnn = keras.layers.Lambda(lambda x: x[..., 0:2])(cnn_inputs)
    else:
        cnn = keras.layers.Lambda(lambda x: x[..., 0:1])(cnn_inputs)

    for _ in range(nlayers):
        for _ in range(nconv):
            cnn = keras.layers.BatchNormalization()(cnn)
            cnn = keras.layers.Conv2D(nfilters, (3, 3), padding='same', activation='relu')(cnn)
        cnn = keras.layers.MaxPool2D()(cnn)
    for _ in range(nlayers):
        cnn = keras.layers.UpSampling2D()(cnn)
        for _ in range(nconv):
            cnn = keras.layers.BatchNormalization()(cnn)
            cnn = keras.layers.Conv2D(nfilters, (3, 3), padding='same', activation='relu')(cnn)

    cnn = keras.layers.core.Reshape((image_size * image_size, -1))(cnn)
    cnn = keras.layers.core.Permute((2, 1))(cnn)
    if symmetric:
        body_zones = keras.layers.Lambda(lambda x: x[..., 2:])(cnn_inputs)
    else:
        body_zones = keras.layers.Lambda(lambda x: x[..., 1:])(cnn_inputs)
    body_zones = keras.layers.core.Reshape((image_size * image_size, -1))(body_zones)
    zone_features = keras.layers.core.Lambda(lambda x: K.batch_dot(x[0], x[1]))([cnn, body_zones])
    image_model = keras.models.Model(cnn_inputs, zone_features)
    return image_model


def _global_model(nfilters, nconv, nlayers, image_size, default_pred, symmetric):
    K.set_learning_phase(1)

    cnn = _global_cnn(nfilters, nconv, nlayers, image_size, symmetric)

    inputs = keras.layers.Input(shape=(4, image_size, image_size, 18+symmetric))
    all_zone_features = keras.layers.wrappers.TimeDistributed(cnn)(inputs)
    all_zone_features = keras.layers.core.Reshape((-1, 17))(all_zone_features)
    all_zone_features = keras.layers.core.Permute((2, 1))(all_zone_features)

    predictions = keras.layers.Dropout(0.5)(all_zone_features)
    predictions = SplitDenseLayer(default_pred)(predictions)
    predictions = keras.layers.Activation(keras.activations.sigmoid)(predictions)

    model = keras.models.Model(inputs=inputs, outputs=predictions)
    return model


def _siamese_model(nfilters, nconv, nlayers, image_size):
    K.set_learning_phase(1)

    pair_input = keras.layers.Input(shape=(image_size, image_size, 19))
    image_1 = keras.layers.Lambda(lambda x: x[..., 0:1])(pair_input)
    image_2 = keras.layers.Lambda(lambda x: x[..., 1:2])(pair_input)
    zones = keras.layers.Lambda(lambda x: x[..., 2:])(pair_input)
    input_1 = keras.layers.Concatenate()([image_1, zones])
    input_2 = keras.layers.Concatenate()([image_2, zones])

    cnn = _global_cnn(nfilters, nconv, nlayers, image_size, False)
    features_1 = keras.layers.Reshape((-1, 17, 1))(cnn(input_1))
    features_2 = keras.layers.Reshape((-1, 17, 1))(cnn(input_2))
    pair_output = keras.layers.Concatenate()([features_1, features_2])

    pair_model = keras.models.Model(inputs=pair_input, outputs=pair_output)

    inputs = keras.layers.Input(shape=(4, image_size, image_size, 19))
    features = keras.layers.TimeDistributed(pair_model)(inputs)
    features = keras.layers.core.Reshape((-1, 17, 2))(features)
    features = keras.layers.core.Permute((2, 3, 1))(features)

    preds = keras.layers.Lambda(lambda x: K.mean(K.square(x[..., 0, :] - x[..., 1, :]), axis=-1))(features)
    preds = keras.layers.BatchNormalization()(preds)
    preds = keras.layers.Activation(keras.activations.sigmoid)(preds)

    model = keras.models.Model(inputs=inputs, outputs=preds)
    return model


def _siamese_preds(y):
    idx = np.array([3, 4, 1, 2, 5, 7, 6, 10, 9, 8, 12, 11, 14, 13, 16, 15, 17]) - 1
    return y == y[:, idx]


@cached(get_augmented_global_image_train_data, version=0)
def train_global_siamese_2d_cnn_model(mode, image_size):
    assert mode in ('train', 'sample_train')

    batch_size = 32
    if not os.path.exists('model.h5'):
        train = 'train' if mode == 'train' else 'sample_train'
        valid = 'valid' if mode == 'train' else 'sample_valid'

        if mode == 'train':
            epochs = 5
        else:
            epochs = 10

        x_train, y_train = get_augmented_global_image_train_data(train, image_size, True)
        x_valid, y_valid = get_augmented_global_image_train_data(valid, image_size, True)
        y_train, y_valid = _siamese_preds(y_train[()]), _siamese_preds(y_valid[()])
        y_mean = np.mean(y_train)
        print('default loss = %s' % (-(y_mean*np.log(y_mean) + (1-y_mean)*np.log(1-y_mean))))

        model = _siamese_model(32, 2, 3, image_size)
        optimizer = keras.optimizers.Adam(1e-1)
        model.compile(optimizer=optimizer, loss='binary_crossentropy')

        model.fit(x_train, y_train, epochs=epochs, batch_size=batch_size, shuffle=False)

        valid_loss = model.evaluate(x_valid, y_valid)
        with open('performance.txt', 'w') as f:
            f.write(str(valid_loss))
        model.save('model.h5')
    else:
        K.set_learning_phase(1)
        model = keras.models.load_model('model.h5')

    return model


@cached(get_augmented_global_image_train_data, version=3)
def train_global_2d_cnn_model(mode, image_size, symmetric):
    assert mode in ('train', 'sample_train')

    batch_size = 32
    if not os.path.exists('model.h5'):
        train = 'train' if mode == 'train' else 'sample_train'
        valid = 'valid' if mode == 'train' else 'sample_valid'

        repeat_batch = 5
        if mode == 'train':
            epochs = 1
        else:
            epochs = 10

        x_train, y_train = get_augmented_global_image_train_data(train, image_size, symmetric)
        x_valid, y_valid = get_augmented_global_image_train_data(valid, image_size, symmetric)
        y_train, y_valid = y_train[()], y_valid[()]

        model = _global_model(32, 2, 3, image_size, np.mean(y_train), symmetric)
        optimizer = keras.optimizers.Adam(1e-4)
        model.compile(optimizer=optimizer, loss='binary_crossentropy')

        for epoch in tqdm.trange(epochs, desc='epochs'):
            chunk_size = int(10e9) // x_train[0, ...].nbytes
            for i in tqdm.trange(0, len(x_train), chunk_size, desc='chunks'):
                x_batch, y_batch = x_train[i:i+chunk_size], y_train[i:i+chunk_size]
                for batch in range(repeat_batch):
                    model.fit(x_batch, y_batch, batch_size=batch_size)

        valid_loss = model.evaluate(x_valid, y_valid)
        with open('performance.txt', 'w') as f:
            f.write(str(valid_loss))
        model.save('model.h5')
    else:
        K.set_learning_phase(1)
        model = keras.models.load_model('model.h5', custom_objects={'SplitDenseLayer': SplitDenseLayer})

    return model


@cached(get_augmented_global_image_test_data, train_global_2d_cnn_model, version=2)
def get_global_2d_cnn_test_predictions(mode):
    assert mode in ('test', 'sample_test')

    if not os.path.exists('ret.pickle'):
        model = train_global_2d_cnn_model('train' if mode == 'test' else 'sample_train', 128, False)
        x, files = get_augmented_global_image_test_data(mode, 128)

        y = model.predict(x, batch_size=32)
        y = np.mean(np.reshape(y, (-1, len(x), 17)), axis=0)
        ret = {x:y for x, y in zip(files, y)}

        with open('ret.pickle', 'wb') as f:
            pickle.dump(ret, f)
    else:
        with open('ret.pickle', 'rb') as f:
            ret = pickle.load(f)
    return ret


@cached(get_local_2d_cnn_test_predictions, version=0)
def write_local_2d_cnn_test_predictions(mode):
    preds = get_local_2d_cnn_test_predictions(mode)
    dataio.write_answer_csv(preds)


@cached(get_global_2d_cnn_test_predictions, version=0)
def write_global_2d_cnn_test_predictions(mode):
    preds = get_global_2d_cnn_test_predictions(mode)
    for pred in preds:
        preds[pred] = np.clip(preds[pred], 0.025, 0.975)
    dataio.write_answer_csv(preds)