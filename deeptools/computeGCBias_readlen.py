#!/usr/bin/env python
# -*- coding: utf-8 -*-

import time

import multiprocessing
import numpy as np
import pandas as pd
import argparse
from scipy.stats import poisson
import py2bit
import sys
import logging
import math

from csaps import csaps
from deeptoolsintervals import GTF
from deeptools.utilities import tbitToBamChrName, getGC_content
from deeptools import parserCommon, mapReduce
from deeptools import bamHandler

debug = 0
old_settings = np.seterr(all='ignore')
global_vars = {}


def parse_arguments():
    parent_parser = parserCommon.getParentArgParse(binSize=False, blackList=True)
    required_args = getRequiredArgs()
    parser = argparse.ArgumentParser(
        parents=[required_args, parent_parser],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description='Computes the GC-bias using Benjamini\'s method '
                    '[Benjamini & Speed (2012). Nucleic Acids Research, 40(10). doi: 10.1093/nar/gks001]. '
                    'The GC-bias is visualized and the resulting table can be used to'
                    'correct the bias with `correctGCBias`.',
        usage='\n computeGCBias -b file.bam --effectiveGenomeSize 2150570000 -g mm9.2bit -l 200 '
              '--GCbiasFrequenciesFile freq.txt [options]',
        conflict_handler='resolve',
        add_help=False)
    return parser


def getRequiredArgs():
    parser = argparse.ArgumentParser(add_help=False)

    required = parser.add_argument_group('Required arguments')

    required.add_argument('--bamfile', '-b',
                          metavar='bam file',
                          help='Sorted BAM file. ',
                          required=True)
    required.add_argument('--effectiveGenomeSize',
                          help='The effective genome size is the portion '
                               'of the genome that is mappable. Large fractions of '
                               'the genome are stretches of NNNN that should be '
                               'discarded. Also, if repetitive regions were not '
                               'included in the mapping of reads, the effective '
                               'genome size needs to be adjusted accordingly. '
                               'A table of values is available here: '
                               'http://deeptools.readthedocs.io/en/latest/content/feature/effectiveGenomeSize.html .',
                          default=None,
                          type=int,
                          required=True)
    required.add_argument('--genome', '-g',
                          help='Genome in two bit format. Most genomes can be '
                               'found here: http://hgdownload.cse.ucsc.edu/gbdb/ '
                               'Search for the .2bit ending. Otherwise, fasta '
                               'files can be converted to 2bit using the UCSC '
                               'programm called faToTwoBit available for different '
                               'plattforms at '
                               'http://hgdownload.cse.ucsc.edu/admin/exe/',
                          metavar='2bit FILE',
                          required=True)
    required.add_argument('--GCbiasFrequenciesFile', '-freq', '-o',
                          help='Path to save the file containing '
                               'the observed and expected read frequencies per %%GC-'
                               'content. This file is needed to run the '
                               'correctGCBias tool. This is a text file.',
                          type=argparse.FileType('w'),
                          metavar='FILE',
                          required=True)
    # define the optional arguments
    optional = parser.add_argument_group('Optional arguments')
    optional.add_argument('--minLength', '-min',
                          default=30,
                          help='Minimum fragment length to consider for bias computation.'
                               '(Default: %(default)s)',
                          type=int)
    optional.add_argument('--maxLength', '-max',
                          default=250,
                          help='Maximum fragment length to consider for bias computation.'
                               '(Default: %(default)s)',
                          type=int)
    optional.add_argument('--lengthStep', '-fstep',
                          default=5,
                          help='Step size for fragment lenghts between minimum and maximum fragment length.'
                               'Will be ignored if interpolate is set.'
                               '(Default: %(default)s)',
                          type=int)
    optional.add_argument("--interpolate", "-I",
                          help='Interpolates GC values and correction for missing read lengths.'
                               'This might substantially reduce computation time, but might lead to'
                               'less accurate results. Deactivated by default.',
                          action='store_true')
    optional.add_argument("--MeasurementOutput", "-MO",
                          help='Writes measured values to an output file.'
                               'This option is only active is Interpolation is activated.',
                          type=argparse.FileType('w'),
                          metavar='FILE')
    optional.add_argument("--help", "-h", action="help",
                          help="show this help message and exit")
    optional.add_argument('--sampleSize',
                          default=5e7,
                          help='Number of sampling points to be considered. (Default: %(default)s)',
                          type=int)
    optional.add_argument('--extraSampling',
                          help='BED file containing genomic regions for which '
                               'extra sampling is required because they are '
                               'underrepresented in the genome.',
                          type=argparse.FileType('r'),
                          metavar='BED file')
    optional.add_argument('--debug', '-d', dest='debug', action='store_true',
                          help='Flag: if set, debugging output will be included in commandline output and logging.')

    #    plot = parser.add_argument_group('Diagnostic plot options')
    #
    #    plot.add_argument('--biasPlot',
    #                      metavar='FILE NAME',
    #                      help='If given, a diagnostic image summarizing '
    #                      'the GC-bias will be saved.')
    #
    #    plot.add_argument('--plotFileFormat',
    #                      metavar='',
    #                      help='image format type. If given, this '
    #                      'option overrides the '
    #                      'image format based on the plotFile ending. '
    #                      'The available options are: "png", '
    #                      '"eps", "pdf", "plotly" and "svg"',
    #                      choices=['png', 'pdf', 'svg', 'eps', 'plotly'])
    #
    #    plot.add_argument('--regionSize',
    #                      metavar='INT',
    #                      type=int,
    #                      default=300,
    #                      help='To plot the reads per %%GC over a region'
    #                      'the size of the region is required. By default, '
    #                      'the bin size is set to 300 bases, which is close to the '
    #                      'standard fragment size for Illumina machines. However, '
    #                      'if the depth of sequencing is low, a larger bin size '
    #                      'will be required, otherwise many bins will not '
    #                      'overlap with any read (Default: %(default)s)')
    #
    return parser


rng = np.random.default_rng()


def roundGCLenghtBias(gc):
    gc_frac, gc_int = math.modf(round(gc * 100, 2))
    gc_new = gc_int + rng.binomial(1, gc_frac)
    return int(gc_new)


def getPositionsToSample(chrom, start, end, stepSize):
    """
    check if the region submitted to the worker
    overlaps with the region to take extra effort to sample.
    If that is the case, the regions to sample array is
    increased to match each of the positions in the extra
    effort region sampled at the same stepSize along the interval.

    If a filter out tree is given, then from positions to sample
    those regions are cleaned
    """
    global debug
    positions_to_sample = np.arange(start, end, stepSize)

    if global_vars['filter_out']:
        filter_out_tree = global_vars['filter_out']
        # filter_out_tree = GTF(global_vars['filter_out']) #moved to main to remove repetition
    else:
        filter_out_tree = None

    if global_vars['extra_sampling_file']:
        extra_tree = global_vars['extra_sampling_file']
        # extra_tree = GTF(global_vars['extra_sampling_file'])#moved to main to remove repetition
    else:
        extra_tree = None

    if extra_tree:
        orig_len = len(positions_to_sample)
        try:
            extra_match = extra_tree.findOverlaps(chrom, start, end)
        except KeyError:
            extra_match = []

        if len(extra_match) > 0:
            for intval in extra_match:
                positions_to_sample = np.append(positions_to_sample,
                                                list(range(intval[0], intval[1], stepSize)))
        # remove duplicates
        positions_to_sample = np.unique(np.sort(positions_to_sample))
        if debug:
            logging.debug(f"sampling increased to {len(positions_to_sample)} from {orig_len}")

    # skip regions that are filtered out
    if filter_out_tree:
        try:
            out_match = filter_out_tree.findOverlaps(chrom, start, end)
        except KeyError:
            out_match = []

        if len(out_match) > 0:
            for intval in out_match:
                positions_to_sample = \
                    positions_to_sample[(positions_to_sample < intval[0]) | (positions_to_sample >= intval[1])]
    return positions_to_sample


def tabulateGCcontent_wrapper(args):
    #    print("ARGS:")
    #    print(args)
    return tabulateGCcontent_worker(*args)


def tabulateGCcontent_worker(chromNameBam, start, end, stepSize,
                             fragmentLengths,
                             chrNameBamToBit, verbose=False):
    r""" given genome regions, the GC content of the genome is tabulated for
    fragments of length 'fragmentLength' each 'stepSize' positions.

    >>> test = Tester()
    >>> args = test.testTabulateGCcontentWorker()
    >>> N_gc, F_gc = tabulateGCcontent_worker(*args)

    The forward read positions are:
    [1,  4,  10, 10, 16, 18]
    which correspond to a GC of
    [1,  1,  1,  1,  2,  1]

    The evaluated position are
    [0,  2,  4,  6,  8, 10, 12, 14, 16, 18]
    the corresponding GC is
    [2,  1,  1,  2,  2,  1,  2,  3,  2,  1]

    >>> print(N_gc)
    [0 4 5 1]
    >>> print(F_gc)
    [0 4 1 0]
    >>> test.set_filter_out_file()
    >>> chrNameBam2bit =  {'2L': 'chr2L'}

    Test for the filter out option
    >>> N_gc, F_gc = tabulateGCcontent_worker('2L', 0, 20, 2,
    ... {'median': 3}, chrNameBam2bit)
    >>> test.unset_filter_out_file()

    The evaluated positions are
    [ 0  2  8 10 12 14 16 18]
    >>> print(N_gc)
    [0 3 4 1]
    >>> print(F_gc)
    [0 3 1 0]

    Test for extra_sampling option
    >>> test.set_extra_sampling_file()
    >>> chrNameBam2bit =  {'2L': 'chr2L'}
    >>> res = tabulateGCcontent_worker('2L', 0, 20, 2,
    ... {'median': 3}, chrNameBam2bit)

    The new positions evaluated are
    [0, 1, 2, 3, 4, 6, 8, 10, 12, 14, 16, 18]
    and the GC is
    [2, 1, 1, 0, 1, 2, 2, 1,  2,  3,  2,  1]
    >>> print(res[0])
    [1 5 5 1]
    >>> print(res[1])
    [0 5 1 0]

    """
    global debug

    logging.debug(f"Worker {multiprocessing.current_process().name} is now running")
    if start > end:
        raise NameError("start %d bigger that end %d" % (start, end))

    chromNameBit = chrNameBamToBit[chromNameBam]

    tbit = py2bit.open(global_vars['2bit'])
    bam = bamHandler.openBam(global_vars['bam'])
    peak = 0
    startTime = time.time()

    sub_Ndict = dict()
    sub_Fdict = dict()
    # if verbose:
    #     print(f"[{time.time() - startTime:.3f}] computing positions to sample")

    for fragmentLength in fragmentLengths:
        logging.debug(f"processing fragmentLength: {fragmentLength}")
        # array to keep track of the GC from regions of length 'fragmentLength'
        # from the genome. The index of the array is used to
        # indicate the gc content. The values inside the
        # array are counts. Thus, if N_gc[10] = 3, that means
        # that 3 regions have a gc_content of 10.
        logging.debug("setting up default arrays")
        sub_n_gc = np.zeros(100 + 1, dtype='int')  # change to percent/fraction -> len
        subF_gc = np.zeros(100 + 1, dtype='int')  # change to percent/fraction -> len

        # substract fragment length to not exceed chrom
        positions_to_sample = getPositionsToSample(chromNameBit,
                                                   start, end - fragmentLength, stepSize)
        logging.debug(f"No. of positions to sample: {len(positions_to_sample)}")
        # read_counts = []  # not used except for a check if length == 0 which always evaluates to true
        # Optimize IO.
        # if the sample regions are far apart from each
        # other is faster to go to each location and fetch
        # the reads found there.
        # Otherwise, if the regions to sample are close to
        # each other, is faster to load all the reads in
        # a large region into memory and consider only
        # those falling into the positions to sample.
        # The following code gets the reads
        # that are at sampling positions that lie close together

        #    if np.mean(np.diff(positions_to_sample)) < 1000:
        #        start_pos = min(positions_to_sample)
        #        end_pos = max(positions_to_sample)
        #        if verbose:
        #            print(f"[{time.time() - startTime:.3f}] caching reads")
        #
        #        counts = np.bincount([r.pos - start_pos
        #                              for r in bam.fetch(chromNameBam, start_pos,
        #                                                 end_pos + 1)
        #                              if not r.is_reverse and not r.is_unmapped and r.pos >= start_pos],
        #                             minlength=end_pos - start_pos + 2)
        #
        #        read_counts = counts[positions_to_sample - min(positions_to_sample)]
        #        if verbose:
        #            print(f"[{time.time() - startTime:.3f}] finish caching reads.")
        #

        count_time = time.time()

        c = 1
        logging.debug("looping over positions_to_sample")
        for index in range(len(positions_to_sample)):
            i = positions_to_sample[index]
            # logging.debug(f"Position being processed: {i}")
            # stop if the end of the chromosome is reached
            # if i + fragmentLength['median'] > tbit.chroms(chromNameBit):
            if i + fragmentLength > tbit.chroms(chromNameBit):
                c_name = tbit.chroms(chromNameBit)
                ifrag = i + fragmentLength
                logging.error(f"Breaking because chrom length exceeded: {ifrag} > {c_name}")
                break

            try:
                # gc = getGC_content(tbit, chromNameBit, int(i), int(i + fragmentLength['median']), fraction=True)
                gc = getGC_content(tbit, chromNameBit, int(i), int(i + fragmentLength), fraction=True)
                # print(f"pre: {gc}")
                gc = roundGCLenghtBias(gc)
                # print(f"post: {gc}")
            except Exception as detail:
                if verbose:
                    logging.exception(detail)
                continue
            # print(gc)

            # count all reads at position 'i'
            # if len(read_counts) == 0:  # case when no cache was done; this clause is always true -> check removed
            # logging.debug(f"aggregating read_counts")
            # num_reads = len([x.pos for x in bam.fetch(chromNameBam, i, i + 1)
            #                 if x.is_reverse is False and x.pos == i])
            read_lst = []
            for read in bam.fetch(chromNameBam, i, i + 1):
                r_len = 0
                if read.pos == i:
                    if read.is_proper_pair and read.next_reference_start > read.pos:
                        r_len = abs(read.template_length)
                    elif not read.is_paired:
                        r_len = read.query_length
                    if r_len == fragmentLength:
                        read_lst.append(read.pos)
            num_reads = len(read_lst)
            # logging.debug("reads counted")
            # else:
            #    num_reads = read_counts[index]
            # logging.debug(f"num_reads = {num_reads}")
            if num_reads >= global_vars['max_reads'][fragmentLength]:
                peak += 1
                continue
            # logging.debug("add values to arrays")
            sub_n_gc[gc] += 1
            subF_gc[gc] += num_reads
            # logging.debug("stuck before verbose")
            if debug:
                if index % 50000 == 0:
                    end_time = time.time()
                    logging.debug("%s processing index %d (%.1f per sec) @ %s:%s-%s stepSize: %s" %
                                  (multiprocessing.current_process().name,
                                   index, index / (end_time - count_time),
                                   chromNameBit, start, end, stepSize))
            c += 1
        logging.debug("finished loop")
        sub_Ndict[str(fragmentLength)] = sub_n_gc
        sub_Fdict[str(fragmentLength)] = subF_gc
        if verbose:
            end_time = time.time()
            logging.debug("%s processed fragmentLenght %d (elapsed time: %.1f sec) @ %s:%s-%s stepSize: %s" %
                          (multiprocessing.current_process().name,
                           fragmentLength, fragmentLength / (end_time - count_time),
                           chromNameBit, start, end, stepSize))
    logging.debug("%s total time %.1f sec @ %s:%s-%s stepSize: %s" % (multiprocessing.current_process().name,
                                                                      (time.time() - startTime), chromNameBit, start,
                                                                      end, stepSize))
    logging.debug("returning values")
    return sub_Ndict, sub_Fdict


def tabulateGCcontent(fragment_lengths, chr_name_bit_to_bam, step_size, chrom_sizes,
                      number_of_processors=None, verbose=False, region=None):
    r"""
    Subdivides the genome or the reads into chunks to be analyzed in parallel
    using several processors. This codes handles the creation of
    workers that tabulate the GC content for small regions and then
    collects and integrates the results
    >>> test = Tester()
    >>> arg = test.testTabulateGCcontent()
    >>> res = tabulateGCcontent(*arg)
    >>> res
    array([[  0.        ,  18.        ,   1.        ],
           [  3.        ,  63.        ,   0.45815996],
           [  7.        , 159.        ,   0.42358185],
           [ 25.        , 192.        ,   1.25278115],
           [ 28.        , 215.        ,   1.25301422],
           [ 16.        , 214.        ,   0.71935396],
           [ 12.        ,  95.        ,   1.21532959],
           [  9.        ,  24.        ,   3.60800971],
           [  3.        ,  11.        ,   2.62400706],
           [  0.        ,   0.        ,   1.        ],
           [  0.        ,   0.        ,   1.        ]])
    """
    global global_vars

    chrNameBamToBit = dict([(v, k) for k, v in chr_name_bit_to_bam.items()])
    chunkSize = int(min(2e6, 4e5 / global_vars['reads_per_bp']))
    chrom_sizes = [(k, v) for k, v in chrom_sizes if k in list(chrNameBamToBit.keys())]
    ndict = dict()
    fdict = dict()

    logging.debug(f"Parameters: {step_size},{fragment_lengths},{chrNameBamToBit},{verbose},{tabulateGCcontent_wrapper},"
                  f"{chrom_sizes},{chunkSize},{number_of_processors},{region},{verbose}.")
    imap_res = mapReduce.mapReduce((step_size,
                                    fragment_lengths, chrNameBamToBit,
                                    verbose),
                                   tabulateGCcontent_wrapper,
                                   chrom_sizes,
                                   genomeChunkLength=chunkSize,
                                   numberOfProcessors=number_of_processors,
                                   region=region,
                                   verbose=verbose)

    for subN_gc, subF_gc in imap_res:
        ndict = {k: ndict.get(k, 0) + subN_gc.get(k, 0) for k in set(ndict) | set(subN_gc)}
        fdict = {k: fdict.get(k, 0) + subF_gc.get(k, 0) for k in set(fdict) | set(subF_gc)}

    # create multi-index dict
    data_dict = {"N_gc": ndict, "F_gc": fdict}
    multi_index_dict = {(i, j): data_dict[i][j]
                        for i in data_dict.keys()
                        for j in data_dict[i].keys()}
    data = pd.DataFrame.from_dict(multi_index_dict, orient="index")
    data.index = pd.MultiIndex.from_tuples(data.index)
    data.index = data.index.set_levels(data.index.levels[-1].astype(int),
                                       level=-1)  # set length index to integer for proper sorting
    data.sort_index(inplace=True)

    return data


def interpolate_ratio_csaps(df, smooth=None, normalized=False):
    # separate hypothetical read density from measured read density
    N_GC = df.loc["N_gc"]
    F_GC = df.loc["F_gc"]

    # get min and max values
    N_GC_min, N_GC_max = np.nanmin(N_GC.index.astype("int")), np.nanmax(N_GC.index.astype("int"))
    F_GC_min, F_GC_max = np.nanmin(F_GC.index.astype("int")), np.nanmax(F_GC.index.astype("int"))

    # sparse grid for hypothetical read density
    N_GC_readlen = N_GC.index.to_numpy(dtype=int)
    N_GC_gc = N_GC.columns.to_numpy(dtype=int)

    # sparse grid for measured read density
    F_GC_readlen = F_GC.index.to_numpy(dtype=int)
    F_GC_gc = F_GC.columns.to_numpy(dtype=int)

    N_f2 = csaps([N_GC_readlen, N_GC_gc], N_GC.to_numpy(), smooth=smooth, normalizedsmooth=normalized)
    F_f2 = csaps([F_GC_readlen, F_GC_gc], F_GC.to_numpy(), smooth=smooth, normalizedsmooth=normalized)

    scaling_dict = dict()
    for i in np.arange(N_GC_min, N_GC_max + 1, 1):
        readlen_tmp = i
        N_tmp = N_f2([readlen_tmp, N_GC_gc])
        F_tmp = F_f2([readlen_tmp, F_GC_gc])
        scaling_dict[i] = int(np.sum(N_tmp) / np.sum(F_tmp))

    # get dense data (full GC and readlen range)
    N_a, N_b = np.meshgrid(np.arange(N_GC_min, N_GC_max + 1, 1), N_GC.columns.to_numpy(dtype=int))
    F_a, F_b = np.meshgrid(np.arange(F_GC_min, N_GC_max + 1, 1), F_GC.columns.to_numpy(dtype=int))
    # convert to 2D coordinate pairs
    N_dense_points = np.stack([N_a.ravel(), N_b.ravel()], -1)

    r_list = list()
    f_list = list()
    n_list = list()
    for i in N_dense_points:
        x = i.tolist()
        scaling = scaling_dict[x[0]]
        if (N_f2(x)).astype(int) > 0 and (F_f2(x)).astype(int) > 0:
            ratio = (int(F_f2(x)) / int(N_f2(x)) * scaling)
        else:
            ratio = 1
        f_list.append(int(F_f2(x)))
        n_list.append(int(N_f2(x)))
        r_list.append(ratio)

    ratio_dense = np.array(r_list).reshape(N_a.shape).T
    F_dense = np.array(f_list).reshape(N_a.shape).T
    N_dense = np.array(n_list).reshape(N_a.shape).T

    # create indices for distributions
    ind_N = pd.MultiIndex.from_product([["N_gc"], np.arange(N_GC_min, N_GC_max + 1, 1)])
    ind_F = pd.MultiIndex.from_product([["F_gc"], np.arange(N_GC_min, N_GC_max + 1, 1)])
    ind_R = pd.MultiIndex.from_product([["R_gc"], np.arange(N_GC_min, N_GC_max + 1, 1)])
    # numpy to dataframe with indices
    NInt_df = pd.DataFrame(N_dense, columns=N_GC.columns, index=ind_N)
    FInt_df = pd.DataFrame(F_dense, columns=N_GC.columns, index=ind_F)
    RInt_df = pd.DataFrame(ratio_dense, columns=N_GC.columns, index=ind_R)

    return pd.concat([NInt_df, FInt_df, RInt_df])  # NInt_df.append(FInt_df).append(RInt_df)


def get_ratio(df):
    # separate hypothetical read density from measured read density
    N_GC = df.loc["N_gc"]
    F_GC = df.loc["F_gc"]
    # get min and max values
    # N_GC_min, N_GC_max = np.nanmin(N_GC.index.astype("int")), np.nanmax(N_GC.index.astype("int"))  # not used
    # F_GC_min, F_GC_max = np.nanmin(F_GC.index.astype("int")), np.nanmax(F_GC.index.astype("int"))  # not used

    scaling_dict = dict()
    for i in N_GC.index:
        n_tmp = N_GC.loc[i].to_numpy()
        f_tmp = F_GC.loc[i].to_numpy()
        scaling_dict[i] = float(np.sum(n_tmp)) / float(np.sum(f_tmp))

    r_dict = dict()
    for i in N_GC.index:
        scaling = scaling_dict[i]
        f_gc_t = F_GC.loc[i]
        n_gc_t = N_GC.loc[i]
        r_gc_t = np.array([float(f_gc_t[x]) / n_gc_t[x] * scaling
                           if n_gc_t[x] and f_gc_t[x] > 0 else 1
                           for x in range(len(f_gc_t))])
        r_dict[i] = r_gc_t

    ratio_dense = pd.DataFrame.from_dict(r_dict, orient="index", columns=N_GC.columns)
    ind = pd.MultiIndex.from_product([["R_gc"], ratio_dense.index])
    ratio_dense.index = ind

    return ratio_dense


def main(args=None):
    global global_vars, debug

    args = parse_arguments().parse_args(args)

    debug = args.debug

    if args.extraSampling:
        extra_sampling_file = args.extraSampling.name
        args.extraSampling.close()
    else:
        extra_sampling_file = None

    loglevel = logging.INFO
    log_format = '%(message)s'
    if args.verbose:
        loglevel = logging.DEBUG
        log_format = "%(asctime)s: %(levelname)s - %(message)s"

    logging.basicConfig(stream=sys.stderr, level=loglevel, format=log_format)
    global_vars = dict()
    global_vars['2bit'] = args.genome
    global_vars['bam'] = args.bamfile
    if args.blackListFileName:
        global_vars['filter_out'] = GTF(args.blackListFileName)
    else:
        global_vars['filter_out'] = None
    if args.extraSampling:
        global_vars['extra_sampling_file'] = GTF(extra_sampling_file)
    else:
        global_vars['extra_sampling_file'] = None

    tbit = py2bit.open(global_vars['2bit'])
    bam, mapped, unmapped, stats = bamHandler.openBam(global_vars['bam'], returnStats=True,
                                                      nThreads=args.numberOfProcessors)
    if args.interpolate:
        length_step = args.lengthStep
    else:
        length_step = 1

    fragment_lengths = np.arange(args.minLength, args.maxLength + 1, length_step).tolist()

    chr_name_bit_to_bam = tbitToBamChrName(list(tbit.chroms().keys()), bam.references)

    global_vars['genome_size'] = sum(tbit.chroms().values())
    global_vars['total_reads'] = mapped
    global_vars['reads_per_bp'] = \
        float(global_vars['total_reads']) / args.effectiveGenomeSize

    confidence_p_value = float(1) / args.sampleSize

    # chromSizes: list of tuples
    chrom_sizes = [(bam.references[i], bam.lengths[i])
                   for i in range(len(bam.references))]
    # chromSizes = [x for x in chromSizes if x[0] in tbit.chroms()] # why would you do this?
    # There is a mapping specifically instead of tbit.chroms()

    max_read_dict = dict()
    min_read_dict = dict()
    for fragment_len in fragment_lengths:
        # use poisson distribution to identify peaks that should be discarded.
        # I multiply by 4, because the real distribution of reads
        # vary depending on the gc content
        # and the global number of reads per bp may a be too low.
        # empirically, a value of at least 4 times as big as the
        # reads_per_bp was found.
        # Similarly for the min value, I divide by 4.
        max_read_dict[fragment_len] = poisson(4 * global_vars['reads_per_bp'] * fragment_len).isf(confidence_p_value)
        # this may be of not use, unless the depth of sequencing is really high
        # as this value is close to 0
        min_read_dict[fragment_len] = poisson(0.25 * global_vars['reads_per_bp'] * fragment_len).ppf(confidence_p_value)

    global_vars['max_reads'] = max_read_dict
    global_vars['min_reads'] = min_read_dict

    for key in global_vars:
        logging.debug(f"{key}: {global_vars[key]}")

    logging.info("computing frequencies")
    # the GC of the genome is sampled each stepSize bp.
    step_size = max(int(global_vars['genome_size'] / args.sampleSize), 1)
    logging.info(f"stepSize for genome sampling: {step_size}")

    data = tabulateGCcontent(fragment_lengths,
                             chr_name_bit_to_bam, step_size,
                             chrom_sizes,
                             number_of_processors=args.numberOfProcessors,
                             verbose=args.verbose,
                             region=args.region)
    # change the way data is handled
    if args.interpolate:
        if args.MeasurementOutput:
            logging.info("saving measured data")
            data.to_csv(args.MeasurementOutput.name, sep="\t")
        r_data = interpolate_ratio_csaps(data)
        r_data.to_csv(args.GCbiasFrequenciesFile.name, sep="\t")
    else:
        if args.MeasurementOutput:
            logging.info("Option MeasurementOutput has no effect. Measured data is saved in GCbiasFrequencies file!")
        r_data = get_ratio(data)
        out_data = data.append(r_data)
        out_data.to_csv(args.GCbiasFrequenciesFile.name, sep="\t")


if __name__ == "__main__":
    main()
