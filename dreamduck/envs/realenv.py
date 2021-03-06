import numpy as np
import argparse
from pyglet.window import key
import sys
import pyglet
from gym import spaces
from gym.spaces.box import Box
from dreamduck.envs.env import DuckieTownWrapper
from dreamduck.envs.rnn.rnn import reset_graph, rnn_model_path_name, \
    model_rnn_size, MDNRNN, hps_sample, get_pi_idx, model_state_space
from dreamduck.envs.vae.vae import ConvVAE, vae_model_path_name
import os
from cv2 import resize
from gym.utils import seeding
import tensorflow as tf
from dreamduck.envs.util import _process_frame

SCREEN_X = 64
SCREEN_Y = 64
DEBUG = False
DEBUG_NEXT = False
TEMPERATURE = 0.8


class DuckieTownReal(DuckieTownWrapper):
    """World Model Representation"""

    def __init__(self, render_mode=True, load_model=True):
        super(DuckieTownReal, self).__init__()

        reset_graph()
        self.vae = ConvVAE(batch_size=1, gpu_mode=tf.test.is_gpu_available(),
                           is_training=False, reuse=True)
        self.rnn = MDNRNN(hps_sample, gpu_mode=tf.test.is_gpu_available())

        if load_model:
            self.vae.load_json(os.path.join(vae_model_path_name, 'vae.json'))
            self.rnn.load_json(os.path.join(rnn_model_path_name, 'rnn.json'))

        self.action_space = spaces.Box(low=-1.0, high=1.0, shape=(2,))
        self.outwidth = self.rnn.hps.output_seq_width
        self.obs_size = self.outwidth + model_rnn_size*model_state_space
        self.observation_space = Box(
            low=-50., high=50., shape=(self.obs_size,))

        self.zero_state = self.rnn.sess.run(self.rnn.initial_state)
        self._seed()

        self.render_mode = render_mode
        self.temperature = TEMPERATURE
        self.rnn_state = None
        self.z = None
        self.current_obs = None
        self.next_z = np.zeros((1, 1, self.outwidth))
        self.frame_count = None
        self.viewer = None
        self._reset()

    def _step(self, action):
        self.frame_count += 1

        prev_z = np.zeros((1, 1, self.outwidth))
        prev_z[0][0] = self.z
        prev_action = np.reshape(action, (1, 1, 2))
        input_x = np.concatenate((prev_z, prev_action), axis=2)
        s_model = self.rnn

        feed = {s_model.input_x: input_x,
                s_model.initial_state: self.rnn_state}

        [logmix, mean, logstd, next_state] = \
            s_model.sess.run([s_model.out_logmix,
                              s_model.out_mean,
                              s_model.out_logstd,
                              s_model.final_state],
                             feed)

        self.rnn_state = next_state
        OUTWIDTH = self.outwidth
        # adjust temperatures
        logmix2 = np.copy(logmix)/self.temperature
        logmix2 -= logmix2.max()
        logmix2 = np.exp(logmix2)
        logmix2 /= logmix2.sum(axis=1).reshape(OUTWIDTH, 1)

        mixture_idx = np.zeros(OUTWIDTH)
        chosen_mean = np.zeros(OUTWIDTH)
        chosen_logstd = np.zeros(OUTWIDTH)
        for j in range(OUTWIDTH):
            idx = get_pi_idx(self.np_random.rand(), logmix2[j])
            mixture_idx[j] = idx
            chosen_mean[j] = mean[j][idx]
            chosen_logstd[j] = logstd[j][idx]

        rand_gaussian = self.np_random.randn(
            OUTWIDTH)*np.sqrt(self.temperature)
        self.next_z = chosen_mean+np.exp(chosen_logstd) * rand_gaussian

        obs, reward, done, _ = super(DuckieTownReal, self)._step(action)
        small_obs = _process_frame(obs)
        self.current_obs = small_obs
        self.z = self._encode(small_obs)

        if DEBUG:
            print('DIST', np.linalg.norm(self.z-prev_z), 'DONE:', done)

        return self._current_state(), reward, done, {}

    def _encode(self, img):
        simple_obs = np.copy(img).astype(np.float)/255.0
        simple_obs = simple_obs.reshape(1, SCREEN_X, SCREEN_Y, 3)
        mu, logvar = self.vae.encode_mu_logvar(simple_obs)
        return (mu + np.exp(logvar/2.0)
                * self.np_random.randn(*logvar.shape))[0]

    def _decode(self, z):
        img = self.vae.decode(z.reshape(1, 64)) * 255.
        img = np.round(img).astype(np.uint8)
        img = img.reshape(SCREEN_X, SCREEN_Y, 3)
        return img

    def _reset(self):
        obs = super(DuckieTownReal, self).reset()
        small_obs = _process_frame(obs)
        self.current_obs = small_obs
        self.rnn_state = self.zero_state
        self.z = self._encode(small_obs)
        self.frame_count = 0
        return self._current_state()

    def _current_state(self):
        if model_state_space == 2:
            return np.concatenate([
                self.z, self.rnn_state.c.flatten(), self.rnn_state.h.flatten()
            ], axis=0)
        return np.concatenate([self.z, self.rnn_state.h.flatten()], axis=0)

    def _seed(self, seed=None):
        if seed:
            tf.set_random_seed(seed)
            self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def _render(self, mode='human', close=False):
        if not self.render_mode:
            return
        if close:
            if self.viewer is not None:
                self.viewer.close()
                self.viewer = None
            return
        try:
            small_img = self.current_obs
            if small_img is None:
                small_img = np.zeros(
                    shape=(SCREEN_X, SCREEN_Y, 3), dtype=np.uint8)

            vae_img = resize(self._decode(self.z), (64, 64))
            WINDOW_HEIGHT = 600
            if DEBUG:
                small_img = resize(small_img, (64, 64))
                if DEBUG_NEXT:
                    next_img = resize(self._decode(self.next_z), (64, 64))
                    img = np.concatenate((small_img, next_img), axis=1)
                else:
                    img = np.concatenate((small_img, vae_img), axis=1)
                WINDOW_WIDTH = 1200
            else:
                WINDOW_WIDTH = 800
                img = vae_img
            if mode == 'rgb_array':
                return img
            elif mode == 'human':
                from pyglet import gl, window, image
                if self.window is None:
                    config = gl.Config(double_buffer=False)
                    self.window = window.Window(
                        width=WINDOW_WIDTH,
                        height=WINDOW_HEIGHT,
                        resizable=False,
                        config=config)

                self.window.clear()
                self.window.switch_to()
                self.window.dispatch_events()
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
                gl.glMatrixMode(gl.GL_PROJECTION)
                gl.glLoadIdentity()
                gl.glMatrixMode(gl.GL_MODELVIEW)
                gl.glLoadIdentity()
                gl.glOrtho(0, WINDOW_WIDTH, 0, WINDOW_HEIGHT, 0, 10)
                width = img.shape[1]
                height = img.shape[0]
                img = np.ascontiguousarray(np.flip(img, axis=0))
                from ctypes import POINTER
                img_data = image.ImageData(
                    width,
                    height,
                    'RGB',
                    img.ctypes.data_as(POINTER(gl.GLubyte)),
                    pitch=width * 3,
                )
                img_data.blit(0, 0, 0, width=WINDOW_WIDTH,
                              height=WINDOW_HEIGHT)
        except Exception as e:
            print(e)  # Duckietown has been closed


if __name__ == "__main__":
    env = DuckieTownReal(render_mode=True)
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true',
                        help='Shows world model view next to original obs')
    parser.add_argument('--next-z', action='store_true',
                        help='Shows RNN prediction')
    parser.add_argument('--temp', default=.8, type=float,
                        help='Control uncertainty')
    args = parser.parse_args()
    DEBUG = args.debug
    DEBUG_NEXT = args.next_z
    TEMPERATURE = args.temp
    env._reset()
    env._render()

    @env.unwrapped.window.event
    def on_key_press(symbol, modifiers):
        if symbol == key.BACKSPACE or symbol == key.SLASH:
            print('RESET')
            env._reset()
            env._render()
        elif symbol == (key.PAGEUP or key.SEMICOLON):
            env.unwrapped.cam_angle[0] = 0
        elif symbol == key.ESCAPE:
            env.close()
            sys.exit(0)
    key_handler = key.KeyStateHandler()
    env.unwrapped.window.push_handlers(key_handler)

    def update(dt):
        action = np.array([0.0, 0.0])
        if key_handler[key.UP]:
            action = np.array([0.44, 0.0])
        if key_handler[key.DOWN]:
            action = np.array([-0.44, 0])
        if key_handler[key.LEFT]:
            action = np.array([0.35, +1])
        if key_handler[key.RIGHT]:
            action = np.array([0.35, -1])
        if key_handler[key.SPACE]:
            action = np.array([0, 0])
        # Speed boost
        if key_handler[key.LSHIFT]:
            action *= 1.5
        obs, reward, done, info = env._step(action)
        print('step_count = %s, reward=%.3f' %
              (env.unwrapped.step_count, reward))

        if key_handler[key.RETURN]:
            from PIL import Image
            im = Image.fromarray(obs)
            im.save('screen.png')

        if done:
            print('done!')
            env._reset()
            env._render()

        env._render()

    pyglet.clock.schedule_interval(update, 1.0 / env.unwrapped.frame_rate)

    pyglet.app.run()
    env.close()
