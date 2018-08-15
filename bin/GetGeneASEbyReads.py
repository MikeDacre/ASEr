from __future__ import print_function
from pysam import AlignmentFile
from argparse import ArgumentParser, FileType
from collections import defaultdict, Counter
from multiprocessing import Pool, cpu_count
from scipy.stats import binom_test
from scipy.special import gammaln
from sys import stdout
from os import path
from itertools import chain
import ASEr.logme as lm
from math import log10, log2, sqrt
import pickle
import subprocess
import re

ASSIGNED_READS = {}

try:
    from progressbar import ProgressBar as pbar
except ImportError:
    print("Not loading progress bar")
    pbar = lambda: lambda x: iter(x)

def get_phase(read, snps):
    """ Returns the phase of the read, according to SNPs

    None: No snps falling across the read
    -1: All SNP(s) match Reference alleles
    0: SNPs disagree about which allele this read matches, or read contains non
        REF/ALT
    1: All SNP(s) match Alternate allele
    """

    phase = None
    for read_pos, ref_pos in read.get_aligned_pairs(matches_only=True):
        if ref_pos + 1 in snps and read.query_qualities[read_pos] >= 30:
            if phase == None:
                try:
                    # 1 if alternate, -1 if reference
                    phase = -1 + 2*snps[ref_pos + 1].index(read.seq[read_pos])
                except ValueError:
                    return 0 # This SNP isn't in the dataset
            else:
                try:
                    new_phase = -1 + 2*snps[ref_pos + 1].index(read.seq[read_pos])
                except ValueError:
                    return 0
                if new_phase != phase:
                    return 0 # read seems misphased
    return phase



def get_snps(snpfile):
    snps = defaultdict(dict)
    if path.exists(path.join(path.dirname(snpfile), 'true_hets.tsv')):
        print("using true hets")
        true_hets = {tuple(line.strip().split()):True
                     for line in open(path.join(path.dirname(snpfile), 'true_hets.tsv'))
                    }
    else:
        true_hets = defaultdict(lambda x: True)
    if snpfile.endswith('.bed'):
        for line in open(snpfile):
            chrom, _, start, refalt = line.strip().split()
            if true_hets.get((chrom, start), True):
                snps[chrom][int(start)] = refalt.split('|')

    return snps

def get_gene_coords(gff_file, id_name, feature_type='exon', extra_fields=[]):
    if False and path.exists(gff_file + '.pkl'):
        return pickle.load(open(gff_file + '.pkl', 'rb'))
    gene_coords = defaultdict(lambda : [
        None,
        set(),
        {},
    ])
    for line in open(gff_file):

        if line.startswith("#"):
            continue

        chrom, _, feature, left, right, _, _, _, annot = (
                line.split('\t'))

        if gff_file.endswith('.gff'):
            sep = '='
        elif gff_file.endswith('.gtf'):
            sep = ' '
        else:
            raise ValueError('Annotation file "{}" ends with unknown suffix')

        annot = dict(item.replace('"', '').strip().split(sep)
                     for item in annot.split(';')
                     if item.strip()
                    )

        feature_id = annot.get(id_name, 'MISSING')
        if feature_type != feature and feature_id == "MISSING":
            continue
        for field in extra_fields:
            if (field in annot) and (field not in gene_coords[feature_id][2]):
                gene_coords[feature_id][2][field] = annot[field]
        if feature != feature_type: continue
        if feature_id == 'MISSING':
            lm.log("Can't find {} in line: '{}'".format(
                id_name, line.strip()),
                level='warn'
                )
            continue
        gene_coords[feature_id][0] = chrom
        gene_coords[feature_id][1].add((int(left), int(right)))

    gene_coords_out = {}
    for entry in gene_coords:
        gene_coords_out[entry] = gene_coords[entry]
    #pickle.dump(gene_coords, open(gff_file + '.pkl', 'wb'))
    return gene_coords

def get_ase_by_coords(chrom, coords, samfile, snp_dict):
    left_most = 1e99
    right_most = 0

    for left, right in coords:
        left_most = min(left, left_most)
        right_most = max(right, right_most)
    assert left_most < right_most

    read_results = Counter()
    phases = defaultdict(set)
    snps_on_chrom = snp_dict[chrom]

    for read in samfile.fetch(chrom, left_most, right_most, multiple_iterators=True):
        read_left = read.reference_start
        read_right = read.reference_end

        # Make sure both ends are in an exon for this gene
        left_hit = False
        right_hit = False
        for left, right in coords:
            if left <= read_left <= right:
                left_hit = True
            if left <= read_right <= right:
                right_hit = True
            if left_hit and right_hit:
                break
        else:
            # One of the ends falls outside of the gene, probably indicating an
            # exon of a different gene, but also possibly an expanded 5' or 3'
            # UTR
            read_results['missed exon boundaries'] += 1
            continue
        phase = ASSIGNED_READS.setdefault(read.qname + chr(ord('1')+read.is_read2),
                                          get_phase(read, snps_on_chrom) )
        phases[read.qname].add(phase)
        # Note that pairs of the same read should have the same qname.

    read_counts = Counter()

    for phase_set in phases.values():
        phase_set.discard(None)
        # Removes None if any, so a paired end read with no information doesn't
        # invalidate the end that does have a clear phase.
        if len(phase_set) == 1:
            # Unambiguously phased
            read_counts[phase_set.pop()] += 1
        elif len(phase_set) == 0:
            read_results['no phasing information'] += 1
            read_counts[None] += 1
        else:
            read_results['discordant phase information'] += 1
            read_counts[0] += 1
    return read_counts



def pref_index(ref, alt):
    return (alt-ref)/(alt+ref)

def log2ase(ref, alt):
    return log2(alt/ref)

def log2ase_offset(ref, alt):
    return log2((alt + 5)/(ref+5))

def ratio(ref, alt):
    return alt/ref

def wilson95_pref(ref, alt):
    """Lower bound of the 95% confidence interval

    Calculate the 95% confidence interval of the p, assuming a Bernoulli trial
    that gave the results REF and ALT.  Then, if that interval contains 50%,
    just use that, otherwise take the bound closer to 50%.  Finally, convert to
    a preference index [-1, 1], instead of a probability [0, 1] by multiplying
    by 2, and subtracting 1.

    See https://en.wikipedia.org/wiki/Binomial_proportion_confidence_interval.
    """

    z = 1.96
    n = ref + alt
    phat = alt/n

    plusminus = z * sqrt(1/n * phat * (1-phat) + 1/(4 * n**2) * z**2)

    p_plus = 1/(1+z**2/n) * (phat + z**2/(2*n) + plusminus)
    p_minus = 1/(1+z**2/n) * (phat + z**2/(2*n) - plusminus)

    if p_minus < 0.5 < p_plus:
        p = 0.5
    elif p_minus > 0.5:
        p = p_minus
    elif p_plus < 0.5:
        p = p_plus
    else:
        raise ValueError("I think I really screwed the pooch on this one")

    return 2 * p - 1

def diff_expression_prod(ref, alt):
    return (alt - ref) * (alt + ref)

def pref_index_expression_normed(ref, alt):
    return (alt - ref)/sqrt(alt + ref)
def binom_pval_polarized(ref, alt):
    """A polarized version of the log10 pvalue

    Will be negative if more reference than alternative, positive if more
    alternative than reference"""

    pval = binom_test([ref, alt], p=0.5)
    if ref > alt:
        return log10(pval) if pval>0 else -30
    else:
        return -log10(pval) if pval>0 else 30

def beta_binom(ref, alt, all_ref, all_alt):
    x = alt
    n = ref + alt
    a = all_ref
    b = all_alt
    lnanswer = gammaln(n+1) + gammaln(x+a) + gammaln(n-x+b) + gammaln(a+b) - \
            (gammaln(x+1) + gammaln(n-x+1) + gammaln(a) + gammaln(b) + gammaln(n+a+b))
    return lnanswer


ase_fcns = {
    'wilson95': wilson95_pref,
    'pref_index': pref_index,
    'log2': log2ase,
    'ratio': ratio,
    'pref_expr': pref_index_expression_normed,
    'diff_expr': diff_expression_prod,
    'log10pval': binom_pval_polarized,
    'log2offset': log2ase_offset,
}

def get_lib_size(reads):
    arg = "samtools idxstats " + reads + " | awk -F \'\t\' \'{s+=$3+$4}END{print s}\'"
    libSize = str(subprocess.check_output(arg,shell=True))
    libSize=re.sub(r'b\'([0-9]+)(\\.*)',r'\1',libSize)
    return libSize

def parse_args():
    parser = ArgumentParser()
    parser.add_argument('snp_file')
    parser.add_argument('gff_file')
    parser.add_argument('reads')
    parser.add_argument('--assign-all-reads', default=False,
            action='store_true',
            help='Get an overall accounting of which reads map to reference, '
            'alternative, or other')
    parser.add_argument('--max-jobs', '-p', default=0, type=int,
            help=''
            )
    parser.add_argument('--id-name', '-n', default='gene_id', type=str)
    parser.add_argument('--outfile', '-o', default=stdout,
            type=FileType('w'),
            )
    parser.add_argument('--min-reads-per-gene', '-m', default=20, type=int)
    parser.add_argument('--ase-function', '-f', default='log2', type=str)
    parser.add_argument('--min-reads-per-allele', '-M', default=0, type=int)
    parser.add_argument('--print-coords', '-C', default=False,
                        action='store_true')
    parser.add_argument('--extra-fields', default=[], nargs='*')

    args = parser.parse_args()
    if args.ase_function not in ase_fcns:
        print("Unrecognized function: {}".format(args.ase_fcn))
        raise ValueError
    return args

if __name__ == "__main__":
    args = parse_args()
    reads = AlignmentFile(args.reads,'rb')
    snp_dict = get_snps(args.snp_file)
    gene_coords = get_gene_coords(args.gff_file, args.id_name, extra_fields = args.extra_fields)
    lib_size = get_lib_size(args.reads)
    ase_vals = {}
    if False and args.max_jobs != 1:
        # Early experiments suggest this doesn't actually make things faster, so
        # the if False automatically skips this branch.  But if someone later
        # wants to put it back i, it should be easy...
        with Pool(args.max_jobs or cpu_count()) as pool:
            for gene in gene_coords:
                ase_vals[gene] = pool.apply_async(
                        get_ase_by_coords,
                        (
                            gene_coords[gene][0],
                            gene_coords[gene][1],
                            reads,
                            snp_dict,
                            ))

            prog = pbar()
            for gene in prog(ase_vals):
                ase_vals[gene] = ase_vals[gene].get()
            if 'finish' in dir(prog):
                prog.finish()
    else:
        if args.assign_all_reads:
            phase_counters = Counter()
            references = list(reads.references)
            unpaired_reads = [{}, {}] #Unpaired Read 1s, Unpaired Read 2s
            for read in pbar(max_value=reads.mapped)(reads):
                qname = read.qname
                phase = get_phase(
                        read,
                        snp_dict[references[read.reference_id]]
                        )
                if qname in unpaired_reads[read.is_read1]:
                    other_phase = unpaired_reads[read.is_read1].pop(qname)
                    phase_set = set([phase, other_phase])
                    phase_set.discard(None)
                    # Removes None if any, so a paired end read with no information doesn't
                    # invalidate the end that does have a clear phase.
                    if len(phase_set) == 1:
                        # Unambiguously phased
                        phase_counters[phase_set.pop()] += 1
                    elif len(phase_set) == 0:
                        phase_counters[None] += 1
                    else:
                        phase_counters[0] += 1
                else:
                    #unpaired_reads[True] = Read 2
                    #unpaired_reads[False] = Read 1
                    unpaired_reads[read.is_read2][qname] = phase


            print(len(unpaired_reads[0]), " unpaired read 1s")
            print(len(unpaired_reads[1]), " unpaired read 2s")

            for phase in chain(unpaired_reads[0].values(),
                    unpaired_reads[1].values()):
                phase_counters[phase] += 1
            print("# All read phases: ", phase_counters, file=args.outfile,
                    end='\n')
            args.outfile.flush()

        prog = pbar()
        chroms = set(reads.references)
        for gene in prog(gene_coords):
            if gene_coords[gene][0] not in chroms:
                lm.log("Can't find {} in the SAM/BAM file".format(
                    gene_coords[gene][0]
                    ),
                    level='warn',
                )
                continue
            ase_vals[gene] = get_ase_by_coords(
                    gene_coords[gene][0],
                    gene_coords[gene][1],
                    reads,
                    snp_dict
                    )
        if 'finish' in dir(prog):
            prog.finish()
    print("# Library size: " + str(lib_size), file=args.outfile, end='\n')
    columns = ['gene', 'chrom', 'ref_counts', 'alt_counts', 'no_ase_counts',
            'ambig_ase_counts', 'ase_value',]
    columns.extend(args.extra_fields)
    if args.print_coords:
        columns.insert(2, 'coords')
    print(
        *columns,
        file=args.outfile, sep='\t', end='\n'
    )
    for gene in sorted(ase_vals):
        avg = ase_vals[gene]
        # Not "average", "ase vals for gene"
        if (min(avg[1], avg[-1]) >= args.min_reads_per_allele) and (avg[1] + avg[-1] >= args.min_reads_per_gene):
            ase_fcn = ase_fcns[args.ase_function]
            ase_val = ase_fcn(avg[-1], avg[1])
        else:
            ase_val = 'NA'
        out_data = [
                gene, gene_coords[gene][0],
                ase_vals[gene][-1],
                ase_vals[gene][1],
                ase_vals[gene][None],
                ase_vals[gene][0],
                ase_val,]
        out_data += [gene_coords[gene][2].get(field, 'MISSING')
                     for field in args.extra_fields]
        if args.print_coords:
            out_data.insert(2, '{}-{}'.format(
                min(i[0] for i in gene_coords[gene][1]),
                max(i[1] for i in gene_coords[gene][1])
            ))
        print(
            *out_data,
            file=args.outfile, sep='\t', end='\n'
        )

