
import glob
import multiprocessing
import numpy as np
import os
import tqdm
import torch

from score_following_game.data_processing.data_production import SongCache
from score_following_game.data_processing.song import load_song
from score_following_game.data_processing.utils import fluidsynth
from typing import List


class RLScoreFollowPool(object):
    """
        Data Pool for MIDI to MIDI snippet hashing.
    """

    def __init__(self, cache, dataset: str, config: dict, limit_song_steps: int):
        """Constructor.

        Parameters
        ----------
        songs : list
        dataset : str
        config : dict
        """

        # parse config
        self.score_shape = config['score_shape']
        self.perf_shape = config['perf_shape']

        self.target_frame = config['target_frame']
        self.spec_representation = config['spec_representation']
        self.spectrogram_params = config['spectrogram_params']

        self.score_shape = tuple(config['score_shape'])
        self.perf_shape = tuple(config['perf_shape'])

        self.fps = self.spectrogram_params['fps']

        self.dataset = dataset

        self.cache = cache

        self.song_id = None
        self.curr_perf_frame = None
        self.true_score_position = None
        self.est_score_position = None
        self.first_onset = None
        self.last_onset = None
        self.next_onset_idx = 0
        self.current_score_onset_idx = 0

        self.new_position = 0
        self.next_onset = None
        self.current_onset = None
        self.use_rate = config.get('use_rate', True)

        self.curr_song = None

        self.song_history = {}
        self.total_score_len = 0

        self.limit_song_steps = limit_song_steps

    def reset(self, song_index=-1):
        """Reset generator.

        Set sample generator to starting state.
        """

        self.curr_song = self.cache.get_random() if song_index==-1 else self.cache.get_elem(song_index)

        # reset the current performance frame and set the padding for the performance
        self.curr_perf_frame = 0

        self.est_score_position = int(self.curr_song.score['coords_padded'][0])
        self.est_score_position = self.clip_coord(self.est_score_position,
                                                  self.curr_song.score['representation_padded'])
        self.first_score_position = int(self.curr_song.score['coords_padded'][0])
        self.true_score_position = int(self.curr_song.score['coords_padded'][0])

        self.first_onset = int(self.curr_song.get_perf_onset(0))
        debug1 = len(self.curr_song.cur_perf['onsets'])
        self.last_onset = int(self.curr_song.cur_perf['onsets_padded'][-1]) if (self.limit_song_steps is None or self.first_onset+self.limit_song_steps>=len(self.curr_song.cur_perf['onsets'])) else (self.first_onset + self.limit_song_steps)
        print(f'FIRST ONSET={self.first_onset}, LAST ONSET={self.last_onset}')
        if self.first_onset+self.limit_song_steps>=debug1:
            print(f'length onsets: {debug1}, limit={self.limit_song_steps}')
        self.next_onset_idx = 0
        self.next_onset = self.first_onset
        self.new_position = 0
        self.total_score_len = self.curr_song.cur_perf['interpolation_fnc'](self.last_onset) - self.first_score_position

    def get_data(self):
        """ return the np arrays for performance + score to feed into the network 

        Returns results: each element is a tuple of (score excerpt at a frame for a song, audio excerpt at a frame for the song, corresponding score position at a frame for the song)
        """
        results = []
        # print("self.cache.get_maxlen(): ", self.cache.get_maxlen())

        for idx in range(self.cache.get_maxlen()):
            self.reset(idx)  # load song from cache
            frame_idx = 0
            song_arr = []
            while not self.last_onset_reached():  # step through frame by frame
                perf_excerpt, score_excerpt = self.step(frame_idx, dataGen=True)
                normalized_score_pos = (self.true_score_position - self.first_score_position) / self.total_score_len if self.total_score_len != 0 else 0
                song_arr.append((score_excerpt, perf_excerpt, normalized_score_pos))
                frame_idx += 1
            results.append(song_arr)
        return results


    def step(self, perf_frame_idx: int, dataGen=False) -> (np.ndarray, np.ndarray):
        """Perform time step in performance and return state

        Parameters
        ----------
        perf_frame_idx : int
            Frame in the performance.

        Returns
        -------
        perf_representation_excerpt : np.ndarray
            TF-representation of the performance
        score_representation_excerpt : np.ndarray
            TF-representation of the score
        """

        # comes from outside clock (e.g., running audio thread)
        # we always start with the first onset in the performance
        # so we offset the incrementor accordingly
        self.curr_perf_frame = self.first_onset + int(perf_frame_idx)
        perf_frame_idx_pad = self.curr_perf_frame + self.perf_shape[2]

        self.curr_perf_frame = perf_frame_idx_pad

        # update estimated score position
        self.est_score_position = self.new_position
        self.est_score_position = self.clip_coord(self.est_score_position,
                                                  self.curr_song.score['representation_padded'])

        # get true score position from annotations
        self.true_score_position = self.curr_song.get_true_score_position(self.curr_perf_frame)

        # get current piano roll excerpts
        use_pos = self.true_score_position if dataGen else self.est_score_position
        perf_representation_excerpt, score_representation_excerpt = \
            self.curr_song.get_representation_excerpts(perf_frame_idx_pad, use_pos)

        # check if the excerpts have the desired shape
        try:
            assert self.perf_shape == perf_representation_excerpt.shape \
                   and self.score_shape == score_representation_excerpt.shape
        except AssertionError as e:
            print('Datapool encountered a shape mismatch.')
            print('Songname: {}, perf_frame_idx_pad: {}, est_score_position: {}'.format(self.get_current_song_name(),
                                                                                        perf_frame_idx_pad,
                                                                                        self.est_score_position))
            print('Performance: desired shape = {}, actual shape= {}'.format(self.perf_shape,
                                                                             perf_representation_excerpt.shape))
            print('Score: desired shape = {}, actual shape= {}'.format(self.score_shape,
                                                                       score_representation_excerpt.shape))

        if self.curr_perf_frame >= self.next_onset:

            self.next_onset_idx += 1
            self.current_onset = self.next_onset
            if self.next_onset_idx < len(self.curr_song.get_perf_onsets()):
                self.next_onset = self.curr_song.get_perf_onset(self.next_onset_idx)

        return perf_representation_excerpt, score_representation_excerpt

    def get_current_song_timesteps(self):
        return self.curr_song.cur_perf['representation_padded'].shape[-1]

    def update_position(self, newPos=0):
        """update the sheet speed"""
        self.new_position = newPos

    def tracking_error(self):
        """Compute distance between score and performance position."""

        # error should go negative when estimate is behind
        error = self.est_score_position - torch.Tensor(self.true_score_position)
        return error

    def get_true_score_position(self):
        return self.true_score_position

    def last_onset_reached(self):
        """Check if last performance onset is reached."""
        return self.curr_perf_frame >= self.last_onset

    def get_current_song(self):
        """Returns the current song"""
        return self.curr_song

    def get_current_perf_audio_file(self, fs=44100):
        return self.curr_song.get_perf_audio(fs)

    def get_current_score_audio_file(self, fs=44100):
        """Renders the current performance using FluidSynth and returns the waveform."""
        return fluidsynth(self.curr_song.get_score_midi(), fs=fs), fs

    def get_current_song_onsets(self):
        return self.curr_song.get_perf_onsets()

    def get_current_song_name(self):
        return self.curr_song.get_song_name()

    def reached_onset_in_score(self) -> bool:
        """Check if an onset is reached in the score

        Returns
        -------
        True    if the current position within the score is an onset
        False   otherwise
        """
        return self.curr_perf_frame in self.curr_song.score['onsets']

    def get_next_score_onset(self):
        """Get the next onset in the score if the end of the song is not reached

        Returns
        -------
        next_onset : int|None
            the next onset in the score if the end is not reached, otherwise None
        """
        return self.next_onset

    def get_song_history(self):
        return self.song_history

    def clip_coord(self, coord, sheet):
        """
        Clip coordinate to be within sheet bounds
        """

        coord = np.max([coord, self.score_shape[2]//2])
        coord = np.min([coord, sheet.shape[2] - self.score_shape[2]//2 - 1])

        return coord

    def get_total_score_len(self):
        return self.total_score_len

    def get_first_score_position(self):
        return self.first_score_position


def get_shared_cache_pools(cache, config: dict, nr_pools=1, directory='test_sample', limit_song_steps=None) -> List[RLScoreFollowPool]:
    """Get a list of data pools containing all the songs from the directory

    Parameters
    ----------
    cache: SongCache
        shared cache containing the songs the data pools can access
    config : dict
        dictionary specifying the config for the data pool and songs
    nr_pools : int
        number of data pools to create
    directory : str
        path to the directory containing the data that should be loaded

    Returns
    -------
    pools: List[RLScoreFollowPool]
        list of data pools with a shared cache
    """

    pools = [RLScoreFollowPool(cache, os.path.basename(os.path.normpath(directory)), config, limit_song_steps) for _ in range(nr_pools)]

    return pools


def get_data_pools(config: dict, score_folder='score', perf_folder='performance', directory='test_sample',
                   real_perf=None, n_worker=16, limit_song_steps=None) -> List[RLScoreFollowPool]:
    """Get a list of data pools with each data pool containing only a single song from the directory

    Parameters
    ----------
    config : dict
        dictionary specifying the config for the data pool and songs
    score_folder : str
        folder where the score midis are located
    perf_folder : str
        folder where the performance midis are located
    directory : str
        path to the directory containing the data that should be loaded
    real_perf : [None | wav]
        indicates whether to use a real performance (in the form of a wav file) or not
    n_worker : int
        number of workers for concurrently processing the data


    Returns
    -------
    pools : List[RLScoreFollowPool]
        list of data pools
    """

    print('Load data pools...')

    score_paths = list(glob.glob(os.path.join(directory, score_folder, '*.npz')))

    params = [
        dict(
            song_name=os.path.splitext(os.path.basename(os.path.normpath(score_path)))[0],
            config=config,
            score_folder=score_folder,
            perf_folder=perf_folder,
            directory=directory,
            real_perf=real_perf,
            limit_song_steps=limit_song_steps
        )
        for score_path in score_paths
    ]

    pool = multiprocessing.Pool(n_worker)

    data_pools = list(tqdm.tqdm(pool.imap_unordered(get_single_song_pool, params), total=len(score_paths)))

    pool.close()
    return data_pools


def get_single_song_pool(params) -> RLScoreFollowPool:

    config = params['config']
    song_name = params['song_name']
    score_folder = params.get('score_folder', 'score')
    perf_folder = params.get('perf_folder', 'performance')
    directory = params.get('directory', 'test_sample')
    real_perf = params.get('real_perf', None)
    limit_song_steps = params['limit_song_steps']

    cur_path_score = os.path.join(directory, score_folder, song_name + ".npz")
    cur_path_perf = os.path.join(directory, perf_folder, song_name+'.mid')

    song = load_song(config, cur_path_score, cur_path_perf, real_perf=real_perf)

    cache = SongCache(1)
    cache.append(song)

    return RLScoreFollowPool(cache, os.path.basename(os.path.normpath(directory)), config, limit_song_steps)
