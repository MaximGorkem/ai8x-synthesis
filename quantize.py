#!/usr/bin/env python3
###################################################################################################
# Copyright (C) Maxim Integrated Products, Inc. All Rights Reserved.
#
# Maxim Integrated Products, Inc. Default Copyright Notice:
# https://www.maximintegrated.com/en/aboutus/legal/copyrights.html
###################################################################################################
"""
Load contents of a checkpoint files and save them in a format usable for AI84/AI85
"""
import argparse
from functools import partial
import torch
import tornadocnn as tc
import yamlcfg
from devices import device
from distiller.apputils.checkpoint import get_contents_table  # pylint: disable=no-name-in-module

CONV_SCALE_BITS = 8
FC_SCALE_BITS = 8
FC_CLAMP_BITS = 8

DEFAULT_SCALE = .85
DEFAULT_STDDEV = 2.0


def convert_checkpoint(dev, input_file, output_file, arguments):
    """
    Convert checkpoint file or dump parameters for C code
    """
    # Load configuration file
    if arguments.config_file:
        _, _, params = yamlcfg.parse(arguments.config_file, device=dev)
    else:
        params = None

    print("Converting checkpoint file", input_file, "to", output_file)
    checkpoint = torch.load(input_file, map_location='cpu')

    if arguments.verbose:
        print(get_contents_table(checkpoint))

    if arguments.quantized:
        if 'quantizer_metadata' not in checkpoint:
            raise RuntimeError("\nNo quantizer_metadata in checkpoint file.")
        del checkpoint['quantizer_metadata']

    if 'state_dict' not in checkpoint:
        raise RuntimeError("\nNo state_dict in checkpoint file.")

    checkpoint_state = checkpoint['state_dict']

    if arguments.verbose:
        print("\nModel keys (state_dict):\n{}".format(", ".join(list(checkpoint_state.keys()))))

    new_checkpoint_state = checkpoint_state.copy()

    def avg_max(t):
        dim = 0
        view_dims = [t.shape[i] for i in range(dim + 1)] + [-1]
        tv = t.view(*view_dims)
        avg_min, avg_max = tv.min(dim=-1)[0], tv.max(dim=-1)[0]
        return torch.max(avg_min.mean().abs_(), avg_max.mean().abs_())

    def max_max(t):
        return torch.max(t.min().abs_(), t.max().abs_())

    def mean_n_stds_max_abs(t, n_stds=1):
        if n_stds <= 0:
            raise ValueError(f'n_stds must be > 0, got {n_stds}')
        mean, std = t.mean(), t.std()
        min_val = torch.max(t.min(), mean - n_stds * std)
        max_val = torch.min(t.max(), mean + n_stds * std)
        return torch.max(min_val.abs_(), max_val.abs_())

    def get_const(_):
        return arguments.scale

    # Scale to our fixed point representation using any of four methods
    # The 'magic constant' seems to work best!?? FIXME
    if arguments.clip_mode == 'STDDEV':
        sat_fn = partial(mean_n_stds_max_abs, n_stds=arguments.stddev)
        checkpoint['extras']['clipping_method'] = 'STDDEV'
        checkpoint['extras']['clipping_nstds'] = arguments.stddev
    elif arguments.clip_mode == 'MAX':
        sat_fn = max_max
        checkpoint['extras']['clipping_method'] = 'MAX'
    elif arguments.clip_mode == 'AVGMAX':
        sat_fn = avg_max
        checkpoint['extras']['clipping_method'] = 'AVGMAX'
    else:
        sat_fn = get_const
        checkpoint['extras']['clipping_method'] = 'SCALE'
        checkpoint['extras']['clipping_scale'] = arguments.scale
    fc_sat_fn = get_const

    first = True
    layers = 0
    num_layers = len(params['quantization']) if params else None
    for _, k in enumerate(checkpoint_state.keys()):
        operation, parameter = k.rsplit(sep='.', maxsplit=1)
        if parameter in ['w_zero_point', 'b_zero_point']:
            if checkpoint_state[k].nonzero().numel() != 0:
                raise RuntimeError(f"\nParameter {k} is not zero.")
            del new_checkpoint_state[k]
        elif parameter == 'weight':
            if not arguments.quantized:
                module, _ = k.split(sep='.', maxsplit=1)

                if dev != 84 or module != 'fc':
                    if num_layers and layers >= num_layers:
                        continue
                    clamp_bits = None
                    if params is not None:
                        clamp_bits = params['quantization'][layers]
                    if clamp_bits is None:
                        clamp_bits = tc.dev.DEFAULT_WEIGHT_BITS  # Default to 8 bits
                    factor = 2**(clamp_bits-1) * sat_fn(checkpoint_state[k])
                    lower_bound = 0
                    if first:
                        factor /= 2.  # The input layer is [-0.5, +0.5] -- compensate
                        first = False
                else:
                    clamp_bits = arguments.fc
                    lower_bound = 1  # Accomodate ARM q15_t data type when clamping
                    factor = 2**(clamp_bits-1) * fc_sat_fn(checkpoint_state[k])

                if arguments.verbose:
                    print(k, 'avg_max:', avg_max(checkpoint_state[k]),
                          'max:', max_max(checkpoint_state[k]),
                          'mean:', checkpoint_state[k].mean(),
                          'factor:', factor,
                          'bits:', clamp_bits)
                weights = factor * checkpoint_state[k]

                # Ensure it fits and is an integer
                weights = weights.clamp(min=-(2**(clamp_bits-1)-lower_bound),
                                        max=2**(clamp_bits-1)-1).round()

                # Store modified weight/bias back into model
                new_checkpoint_state[k] = weights

                # Is there a bias for this layer? Use the same factor as for weights.
                bias_name = operation + '.bias'
                if bias_name in checkpoint_state:
                    if arguments.verbose:
                        print(' -', bias_name)
                    weights = factor * checkpoint_state[bias_name]

                    # The scale is different for AI84, and this has to happen before clamping.
                    if dev == 84 and module != 'fc':
                        weights *= 2**(clamp_bits-1)

                    # Ensure it fits and is an integer
                    weights = weights.clamp(min=-(2**(clamp_bits-1)-lower_bound),
                                            max=2**(clamp_bits-1)-1).round()

                    # Save conv biases so PyTorch can still use them to run a model. This needs
                    # to be reversed before loading the weights into the AI84/AI85.
                    # When multiplying data with weights, 1.0 * 1.0 corresponds to 128 * 128 and
                    # we divide the output by 128 to compensate. The bias therefore needs to be
                    # multiplied by 128. This depends on the data width, not the weight width,
                    # and is therefore always 128.
                    if dev != 84:
                        weights *= 2**(tc.dev.ACTIVATION_BITS-1)

                    # Store modified weight/bias back into model
                    new_checkpoint_state[bias_name] = weights
            else:
                # Work on a pre-quantized network -- this code is old and probably doesn't work
                # anymore
                module, st = operation.rsplit('.', maxsplit=1)
                if st in ['wrapped_module']:
                    weights = checkpoint_state[k]
                    scale = module + '.' + parameter[0] + '_scale'
                    (scale_bits, clamp_bits) = (CONV_SCALE_BITS, tc.dev.DEFAULT_WEIGHT_BITS) \
                        if dev != 84 or module != 'fc' else (FC_SCALE_BITS, FC_CLAMP_BITS)
                    fp_scale = checkpoint_state[scale]
                    if dev != 84 or module not in ['fc']:
                        # print("Factor in:", fp_scale, "bits", scale_bits, "out:",
                        #       pow2_round(fp_scale, scale_bits))
                        weights *= fp_scale.clamp(min=1, max=2**scale_bits-1).round()
                        # Accomodate Arm q15_t/q7_t datatypes
                        weights = weights.clamp(min=-(2**(clamp_bits-1)),
                                                max=2**(clamp_bits-1)-1).round()
                    else:
                        weights = torch.round(weights * fp_scale)
                        weights = weights.clamp(min=-(2**(clamp_bits-1)-1),
                                                max=2**(clamp_bits-1)-1).round()

                    new_checkpoint_state[module + '.' + parameter] = weights
                    del new_checkpoint_state[k]
                    del new_checkpoint_state[scale]

            if dev != 84 or module != 'fc':
                layers += 1
        elif parameter in ['base_b_q']:
            del new_checkpoint_state[k]

    checkpoint['state_dict'] = new_checkpoint_state
    torch.save(checkpoint, output_file)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Checkpoint to AI8X Quantization')
    parser.add_argument('input', help='path to the checkpoint file')
    parser.add_argument('output', help='path to the output file')
    parser.add_argument('-c', '--config-file', metavar='S',
                        help="optional YAML configuration file containing layer configuration")
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--ai85', action='store_const', const=85, dest='device',
                       help="enable AI85 features (default: AI84)")
    group.add_argument('--ai87', action='store_const', const=87, dest='device',
                       help="enable AI87 features (default: AI84)")
    group.add_argument('--device', type=device, metavar='N',
                       help="set device (default: 84)")
    parser.add_argument('-f', '--fc', type=int, default=FC_CLAMP_BITS, metavar='N',
                        help=f'set number of bits for the fully connnected layers '
                             f'(default: {FC_CLAMP_BITS})')
    parser.add_argument('-q', '--quantized', action='store_true', default=False,
                        help='work on quantized checkpoint')
    parser.add_argument('-v', '--verbose', action='store_true', default=False,
                        help='verbose mode')
    parser.add_argument('--clip-method', default='SCALE', dest='clip_mode',
                        choices=['AVGMAX', 'MAX', 'STDDEV', 'SCALE'],
                        help='saturation clipping method (default: SCALE)')
    parser.add_argument('--scale', type=float,
                        help='set the scale value for the SCALE method (default: magic '
                             f'{DEFAULT_SCALE:.2f})')
    parser.add_argument('--stddev', type=float,
                        help='set the number of standard deviations for the STDDEV method '
                             f'(default: {DEFAULT_STDDEV:.2f})')
    args = parser.parse_args()

    # Configure device
    if not args.device:
        args.device = 84
    if args.clip_mode == 'SCALE' and not args.scale:
        print('WARNING: Using the default scale factor of '
              f'{DEFAULT_SCALE:.2f}.\n')
        args.scale = DEFAULT_SCALE
    if args.clip_mode == 'STDDEV' and not args.stddev:
        print('WARNING: Using the default number of standard deviations of '
              f'{DEFAULT_STDDEV:.2f}.\n')
        args.stddev = DEFAULT_STDDEV
    tc.dev = tc.get_device(args.device)

    convert_checkpoint(args.device, args.input, args.output, args)
