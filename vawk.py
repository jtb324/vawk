#!/usr/bin/env python3.11

import sys
import re
import argparse
from rich_argparse import RichHelpFormatter
from subprocess import Popen, PIPE, STDOUT

__author__ = "Colby Chiang (cc2qe@virginia.edu)"
__version__ = "$Revision: 0.0.2 $"
__date__ = "$Date: 2015-02-28 15:32 $"

# --------------------------------------
# define functions


def get_args():
    parser = argparse.ArgumentParser(
        formatter_class=RichHelpFormatter,
        add_help=True,
        description="\
vawk\n\
author: "
        + __author__
        + "\n\
version: "
        + __version__
        + "\n\
description: An awk-like VCF parser",
    )
    parser.add_argument(
        "-v",
        "--var",
        action="append",
        default=[],
        required=False,
        type=str,
        help="declare an external variable (e.g.: SIZE=10000)",
    )
    parser.add_argument(
        "-c",
        "--col",
        dest="info_col",
        required=False,
        default=8,
        type=int,
        help="column of the INFO field [8]",
    )
    parser.add_argument(
        "--header", required=False, action="store_true", help="print VCF header"
    )
    parser.add_argument(
        "cmd",
        nargs=1,
        help="""vawk command syntax is exactly the same as awk syntax with
    a few additional features. The INFO field can be split using
    the I$ prefix and the SAMPLE field can be split using
    the S$ prefix. For example, I$AF prints the allele frequency of
    each variant and S$NA12878 prints the entire SAMPLE field for the
    NA12878 individual for each variant. S$* returns all samples.

    The SAMPLE field can be further split based on the keys in the
    FORMAT field of the VCF (column 9). For example, S$NA12877$GT
    returns the genotype of the NA12878 individual.

    ex: '{ if (I$AF>0.5) print $1,$2,$3,I$AN,S$NA12878,S$NA12877$GT }'
    """,
    )
    parser.add_argument(
        "vcf", nargs="?", type=str, default=None, help="VCF file (default: stdin)"
    )
    parser.add_argument(
        "--debug", action="store_true", help="debugging level verbosity"
    )

    # parse the arguments
    args = parser.parse_args()

    # if no input, check if part of pipe and if so, read stdin.
    if args.vcf is None:
        if sys.stdin.isatty():
            parser.print_help()
            exit(1)
        else:
            args.vcf = "-"

    # send back the user input
    return args


# parse the vawk string into BEGIN PER-LINE and END portions
def parse(raw):
    begin = ""
    perline = ""
    end = ""
    op_brace = 0
    b = 0
    a = 0
    begin_idx = [0, -1]
    end_idx = [0, -1]

    # get BEGIN script
    try:
        begin_idx[0] = re.search(r"BEGIN\s*{", raw).start()
        a = begin_idx[0] + 5
        b = a
        while begin == "":
            if raw[b] == "{":
                op_brace += 1
            elif raw[b] == "}":
                op_brace -= 1
                if op_brace == 0:
                    begin = raw[a : b + 1].strip()[1:-1]
                    begin_idx[1] = b
                    break
            b += 1
    except AttributeError:
        pass
    raw = raw[: begin_idx[0]] + raw[begin_idx[1] + 1 :]

    # get END script
    try:
        end_idx[0] = re.search(r"END\s*{", raw).start()
        a = re.search(r"END\s*{", raw).start() + 3
        b = a
        while end == "":
            if raw[b] == "{":
                op_brace += 1
            elif raw[b] == "}":
                op_brace -= 1
                if op_brace == 0:
                    end = raw[a : b + 1].strip()[1:-1]
                    end_idx[1] = b
                    break
            b += 1
    except AttributeError:
        pass
    raw = raw[: end_idx[0]] + raw[end_idx[1] + 1 :]

    # get PER-LINE script (remainder of string)
    perline = raw.strip()
    if perline.startswith("{") and perline.endswith("}"):
        perline = perline[1:-1]
    else:
        perline = "if (" + perline + ") print ; "

    return (begin, perline, end)


# a class for the tokenized sample string
class Sample(object):
    def __init__(self, s):
        self.string_raw = s

        # tokenize the string
        s_tok = self.string_raw.split("$")
        self.name_raw = s_tok[0]
        self.name_clean = None
        self.name_escaped = None

        # set the field
        if len(s_tok) == 1:
            self.field = "ALL"
        elif len(s_tok) == 2:
            self.field = s_tok[1]
        else:
            print("Error: format should be S$[ID]$[FMT]")
            exit(1)
        return

    # clean weird punctuation from name
    def clean(self):
        s = self.name_raw
        s_esc = self.name_raw

        gremlin_list = ("-", ".")
        for gremlin in gremlin_list:
            s = s.replace(gremlin, "_")
            s_esc = s_esc.replace(gremlin, "\\" + gremlin)

        self.name_clean = s
        self.name_escaped = s_esc

        return


# primary function
def vawk(debug, header, var, info_col, vawk_string, vcf_file):
    # parse the external variables
    format_col = info_col + 1
    samples_start_col = info_col + 2

    var_list = []
    for v in var:
        var_list = var_list + ["-v", v]

    # parse the vawk string in to BEGIN, END, and PERLINE
    (begin, perline, end) = parse(vawk_string[0])

    if debug:
        print("begin:", begin)
        print("perline:", perline)
        print("end:", end)

    # get the requested info keys from the command line (format: I$FIELD)
    # [\w]: any word character  [a-zA-Z0-9_]
    # [\W]: any non-word character  [^a-zA-Z0-9_]
    info_keys = set(re.findall(r"[\W]I\$([\w]*)", perline))

    if re.search("I\$([^\W]*)", perline):
        # split and parse the relevant INFO fields
        # THE SPLIT INFO MATCHING CAN BE SPED UP BY DOING MULTI GREP AND MOVING THE
        # OR STATEMENT INSIDE
        perline = (
            "split($"
            + str(info_col)
            + ',x,";"); for (i=1;i<=length(x);++i) { '
            + " ".join(
                [
                    'if (x[i]~"^'
                    + mykey
                    + '=" || x[i]=="'
                    + mykey
                    + '") { split(x[i],info,"="); if (length(info)==1) { INFO_'
                    + mykey
                    + "=1} else {INFO_"
                    + mykey
                    + "=info[2]} }"
                    for mykey in info_keys
                ]
            )
            + "} "
            + perline
        )
        # replace the toxic $ in the awk command
        for mykey in info_keys:
            perline = re.sub("I\$" + mykey, "INFO_" + mykey, perline)

    # --------------------------------------
    # sample info
    samples_raw = set(re.findall(r"[\W]S\$([a-zA-Z0-9_\-\.*^$]*)", perline))
    sample_queries = []
    for s in samples_raw:
        # make new Sample object and clean it
        s_obj = Sample(s)
        s_obj.clean()
        # add it to the sample queries
        sample_queries.append(s_obj)

        if debug:
            print("name_raw: " + s_obj.name_raw)
            print("name_clean: " + s_obj.name_clean)
            print("name_escaped: " + s_obj.name_escaped)

    # parse the per-sample data fields
    for s_obj in sample_queries:
        # return all data for each sample
        if s_obj.field == "ALL":
            # concatenate all sample columns with S$*
            if s_obj.name_clean == "*":
                perline = (
                    "ALLSAMPLES_"
                    + s_obj.field
                    + '=""; for (COL='
                    + str(samples_start_col)
                    + ";COL<=NF-1;++COL) { ALLSAMPLES_"
                    + s_obj.field
                    + "=ALLSAMPLES_"
                    + s_obj.field
                    + '""$COL"\\t" }; ALLSAMPLES_'
                    + s_obj.field
                    + "=ALLSAMPLES_"
                    + s_obj.field
                    + '""$NF; '
                    + perline
                )
            # user-specified sample name
            else:
                perline = (
                    "SAMPLE_"
                    + "_".join([s_obj.name_clean, s_obj.field])
                    + '=$SAMPLE["'
                    + s_obj.name_raw
                    + '"]; '
                    + perline
                )
        # return user-specified sample fields
        else:
            # special case for wildcard (all samples)
            if s_obj.name_clean == "*":
                perline = (
                    "ALLSAMPLES_"
                    + s_obj.field
                    + '=""; for(i=1;i<=length(fmt);++i) { if (fmt[i]=="'
                    + s_obj.field
                    + '") { fmt_key=i; break } }; for (COL='
                    + str(samples_start_col)
                    + ';COL<=NF-1;++COL) { split($COL,samp,":"); ALLSAMPLES_'
                    + s_obj.field
                    + "=ALLSAMPLES_"
                    + s_obj.field
                    + '""samp[fmt_key]"\\t" }; split($NF,samp,":"); ALLSAMPLES_'
                    + s_obj.field
                    + "=ALLSAMPLES_"
                    + s_obj.field
                    + '""samp[fmt_key]; '
                    + perline
                )
            else:
                perline = (
                    'split($SAMPLE["'
                    + s_obj.name_raw
                    + '"],samp,":"); for(i=1;i<=length(fmt);++i) { if (fmt[i]=="'
                    + s_obj.field
                    + '") { SAMPLE_'
                    + "_".join([s_obj.name_clean, s_obj.field])
                    + "=samp[i]; break } } "
                    + perline
                )

    # rename query (ex: S$NA12878$GT to SAMPLE_NA12878_GT)
    look_ahead = "(?![$_\-\.a-zA-Z0-9])"
    if re.search("S\$", perline):
        for s_obj in sample_queries:
            if s_obj.field == "ALL":
                if s_obj.name_clean == "*":
                    perline = re.sub(
                        r"S\$\*" + look_ahead, "ALLSAMPLES_" + s_obj.field, perline
                    )
                    perline = perline
                else:
                    perline = re.sub(
                        "S\$" + s_obj.name_escaped + look_ahead,
                        "SAMPLE_" + "_".join([s_obj.name_clean, s_obj.field]),
                        perline,
                    )
            else:
                if s_obj.name_clean == "*":
                    perline = re.sub(
                        "S\$\*\$" + s_obj.field + look_ahead,
                        "ALLSAMPLES_" + s_obj.field,
                        perline,
                    )
                else:
                    perline = re.sub(
                        "S\$"
                        + "\$".join([s_obj.name_escaped, s_obj.field])
                        + look_ahead,
                        "SAMPLE_" + "_".join([s_obj.name_clean, s_obj.field]),
                        perline,
                    )

    # clear data from previous line
    for mykey in info_keys:
        perline = "INFO_" + mykey + '=""; ' + perline
    for s_obj in sample_queries:
        if s_obj.name_clean != "*":
            perline = (
                "SAMPLE_"
                + "_".join([s_obj.name_clean, s_obj.field])
                + '=""; '
                + perline
            )

    # split up the FORMAT field and tack it to the beginning of the per-line statement
    perline = "split($" + str(format_col) + ',fmt,":"); ' + perline

    # print the header if requested
    if header:
        perline = (
            'if ($0~"^#") {if ($0!~"^##") { for (i='
            + str(samples_start_col)
            + ";i<=NF;++i) SAMPLE[$i]=i; }; print} else {"
            + perline
            + "}"
        )
    else:
        perline = (
            'if ($0~"^#") {if ($0!~"^##") { for (i='
            + str(samples_start_col)
            + ";i<=NF;++i) SAMPLE[$i]=i; }; next} else {"
            + perline
            + "}"
        )

    vawk_string = (
        'BEGIN {FS="\\t"; OFS="\\t"; '
        + begin
        + "}"
        + " {"
        + perline
        + "} "
        + "END {"
        + end
        + "}"
    )

    cmd = ["gawk"] + var_list + [vawk_string, vcf_file]
    if debug:
        print(" ".join(cmd))

    p = Popen(cmd, stdout=PIPE, stderr=STDOUT)

    for line in iter(p.stdout.readline, b""):
        print(line.rstrip())

    return


# --------------------------------------
# main function


def main():
    # parse the command line args
    args = get_args()

    # call primary function
    vawk(args.debug, args.header, args.var, args.info_col, args.cmd, args.vcf)


# initialize the script
if __name__ == "__main__":
    try:
        sys.exit(main())
    except IOError as e:
        if e.errno != 32:  # ignore SIGPIPE
            raise e
