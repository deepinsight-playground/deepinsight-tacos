
import tensorflow as tf
import os
from collections import namedtuple
import logging
import os
import click
from numba import jit
import numpy as np
import tensorflow as tf
import yaml
from tqdm import tqdm
import matplotlib.pyplot as plt

from tensorflow_tts.configs import Tacotron2Config
from tensorflow_tts.models import TFTacotron2
from deepinsight_speech.apps.tacotron2.tacotron_dataset import CharactorMelDataset

logging.getLogger(__name__)


@jit(nopython=True)
def get_duration_from_alignment(alignment):
    D = np.array([0 for _ in range(np.shape(alignment)[0])])

    for i in range(np.shape(alignment)[1]):
        max_index = list(alignment[:, i]).index(alignment[:, i].max())
        D[max_index] = D[max_index] + 1

    return D


@click.command(
    description="Extract durations from charactor with trained Tacotron-2 "
    "(See detail in tensorflow_tts/example/tacotron-2/extract_duration.py)."
)
@click.option(
    "--rootdir",
    default=None,
    type=str,
    required=True,
    help="directory including ids/durations files.",
)
@click.option(
    "--outdir", type=str, required=True, help="directory to save generated speech."
)
@click.option(
    "--checkpoint", type=str, required=True, help="checkpoint file to be loaded."
)
@click.option(
    "--use-norm", default=1, type=int, help="usr norm-mels for train or raw."
)
@click.option("--batch-size", default=8, type=int, help="batch size.")
@click.option("--win-front", default=2, type=int, help="win-front.")
@click.option("--win-back", default=2, type=int, help="win-front.")
@click.option(
    "--use-window-mask", default=1, type=int, help="toggle window masking."
)
@click.option("--save-alignment", default=0, type=int, help="save-alignment.")
@click.option(
    "--config",
    default=None,
    type=str,
    required=True,
    help="yaml format configuration file. if not explicitly provided, "
    "it will be searched in the checkpoint directory. (default=None)",
)
@click.option(
    "--verbose",
    type=int,
    default=1,
    help="logging level. higher is more logging. (default=1)",
)
def main(
    root_dir: str, outdir: str, checkpoint: str, 
    use_norm: int, batch_size: int, win_front: int, 
    win_back: int, use_window_mask: int, 
    save_alignment: int, config: str, verbose: int
):
    # check directory existence
    if not os.path.exists(outdir):
        os.makedirs(outdir)

    # load config
    with open(config) as f:
        config = yaml.load(f, Loader=yaml.Loader)

    if config["format"] == "npy":
        char_query = "*-ids.npy"
        mel_query = "*-raw-feats.npy" if use_norm is False else "*-norm-feats.npy"
        char_load_fn = np.load
        mel_load_fn = np.load
    else:
        raise ValueError("Only npy is supported.")

    # define data-loader
    dataset = CharactorMelDataset(
        dataset=config["tacotron2_params"]["dataset"],
        root_dir=root_dir,
        charactor_query=char_query,
        mel_query=mel_query,
        charactor_load_fn=char_load_fn,
        mel_load_fn=mel_load_fn,
        reduction_factor=config["tacotron2_params"]["reduction_factor"],
        use_fixed_shapes=True,
    )
    dataset = dataset.create(allow_cache=True, batch_size=batch_size, drop_remainder=False)

    # define model and load checkpoint
    tacotron2 = TFTacotron2(
        config=Tacotron2Config(**config["tacotron2_params"]),
        name="tacotron2",
    )
    tacotron2._build()  # build model to be able load_weights.
    tacotron2.load_weights(checkpoint)

    # apply tf.function for tacotron2.
    tacotron2 = tf.function(tacotron2, experimental_relax_shapes=True)

    for data in tqdm(dataset, desc="[Extract Duration]"):
        utt_ids = data["utt_ids"]
        input_lengths = data["input_lengths"]
        mel_lengths = data["mel_lengths"]
        utt_ids = utt_ids.numpy()
        real_mel_lengths = data["real_mel_lengths"]
        del data["real_mel_lengths"]

        # tacotron2 inference.
        mel_outputs, post_mel_outputs, stop_outputs, alignment_historys = tacotron2(
            **data,
            use_window_mask=use_window_mask,
            win_front=win_front,
            win_back=win_back,
            training=True,
        )

        # convert to numpy
        alignment_historys = alignment_historys.numpy()

        for i, alignment in enumerate(alignment_historys):
            real_char_length = input_lengths[i].numpy()
            real_mel_length = real_mel_lengths[i].numpy()
            alignment_mel_length = int(
                np.ceil(
                    real_mel_length / config["tacotron2_params"]["reduction_factor"]
                )
            )
            alignment = alignment[:real_char_length, :alignment_mel_length]
            d = get_duration_from_alignment(alignment)  # [max_char_len]

            d = d * config["tacotron2_params"]["reduction_factor"]
            assert (
                np.sum(d) >= real_mel_length
            ), f"{d}, {np.sum(d)}, {alignment_mel_length}, {real_mel_length}"
            if np.sum(d) > real_mel_length:
                rest = np.sum(d) - real_mel_length
                # print(d, np.sum(d), real_mel_length)
                if d[-1] > rest:
                    d[-1] -= rest
                elif d[0] > rest:
                    d[0] -= rest
                else:
                    d[-1] -= rest // 2
                    d[0] -= rest - rest // 2

                assert d[-1] >= 0 and d[0] >= 0, f"{d}, {np.sum(d)}, {real_mel_length}"

            saved_name = utt_ids[i].decode("utf-8")

            # check a length compatible
            assert (
                len(d) == real_char_length
            ), f"different between len_char and len_durations, {len(d)} and {real_char_length}"

            assert (
                np.sum(d) == real_mel_length
            ), f"different between sum_durations and len_mel, {np.sum(d)} and {real_mel_length}"

            # save D to folder.
            np.save(
                os.path.join(outdir, f"{saved_name}-durations.npy"),
                d.astype(np.int32),
                allow_pickle=False,
            )

            # save alignment to debug.
            if save_alignment == 1:
                figname = os.path.join(outdir, f"{saved_name}_alignment.png")
                fig = plt.figure(figsize=(8, 6))
                ax = fig.add_subplot(111)
                ax.set_title(f"Alignment of {saved_name}")
                im = ax.imshow(
                    alignment, aspect="auto", origin="lower", interpolation="none"
                )
                fig.colorbar(im, ax=ax)
                xlabel = "Decoder timestep"
                plt.xlabel(xlabel)
                plt.ylabel("Encoder timestep")
                plt.tight_layout()
                plt.savefig(figname)
                plt.close()


if __name__ == "__main__":
    main()
