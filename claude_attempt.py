#!/usr/bin/env python3
"""
Create missing OPEX files for PAX objects and their XML metadata files.

Expected layout (one stem shown, any number of stems per directory)::

    <stem>.pax/                 PAX directory    -> <stem>.pax.opex
    <stem>_dc.xml               metadata file    -> <stem>_dc.xml.opex
    <stem>_METS.xml             metadata file    -> <stem>_METS.xml.opex
    <stem>_mods.xml             metadata file    -> <stem>_mods.xml.opex

A metadata file belongs to a stem when its name, less the ``.xml`` extension,
is the stem itself or starts with ``<stem>_``. If several stems match (e.g.
``pitt_1`` and ``pitt_1_v2``), the longest one wins. Anything that does not
belong to a PAX directory is ignored.

PAX OPEX:       SourceID = DC identifier (the one that equals the stem when
                ':' is replaced by '_' if there are several), Title = stem,
                Description = DC title, SecurityDescriptor = open, every
                related metadata file embedded in DescriptiveMetadata.
Metadata OPEX:  SourceID = Title = filename less extension,
                Description = DC title, SecurityDescriptor = open.

DC elements are located by namespace, not by file name, so the DC record can
live in a standalone DC file or be embedded in another format (e.g. a METS
dmdSec). Standalone DC records are consulted first.

Existing OPEX files are never modified unless --overwrite is given, so the
tool can be safely re-run after an interruption.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from opex_parser import OpexFileContent, OpexMetadataContent, Writer
from opex_parser.model import OpexPaxContent  # not re-exported by the package
from opex_parser.model_types import HashAlgorithm

log = logging.getLogger("generate_opex")

PAX_SUFFIX = ".pax"
XML_SUFFIX = ".xml"
OPEX_SUFFIX = ".opex"
SECURITY_DESCRIPTOR = "open"

DC_NAMESPACES = (
    "http://purl.org/dc/elements/1.1/",
    "http://purl.org/dc/terms/",
)
# Namespaces whose root element marks a file as a standalone DC record.
DC_RECORD_NAMESPACES = DC_NAMESPACES + ("http://www.openarchives.org/OAI/2.0/oai_dc/",)


class MetadataError(Exception):
    """Required metadata is missing or ambiguous for an object."""


@dataclass
class PaxObject:
    stem: str
    pax_dir: Path
    metadata_files: list[Path] = field(default_factory=list)


@dataclass
class Stats:
    created: int = 0
    existing: int = 0
    failed_objects: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def opex_path_for(path: Path) -> Path:
    """'x.pax' -> 'x.pax.opex', 'x_dc.xml' -> 'x_dc.xml.opex'."""
    return path.with_name(path.name + OPEX_SUFFIX)


def owning_stem(xml_name: str, stems: set[str]) -> str | None:
    """Return the longest stem that the metadata file name belongs to."""
    base = xml_name[: -len(XML_SUFFIX)]
    matches = [s for s in stems if base == s or base.startswith(s + "_")]
    return max(matches, key=len) if matches else None


def discover(directory: Path) -> list[PaxObject]:
    """Group the PAX directories and related metadata files of one directory."""
    with os.scandir(directory) as it:
        entries = sorted(it, key=lambda e: e.name)

    objects: dict[str, PaxObject] = {}
    for entry in entries:
        if (entry.is_dir() and entry.name.endswith(PAX_SUFFIX)
                and len(entry.name) > len(PAX_SUFFIX)):
            stem = entry.name[: -len(PAX_SUFFIX)]
            objects[stem] = PaxObject(stem, Path(entry.path))

    for entry in entries:
        if not (entry.is_file() and entry.name.lower().endswith(XML_SUFFIX)):
            continue
        stem = owning_stem(entry.name, set(objects))
        if stem is None:
            log.debug("ignoring unrelated file %s", entry.path)
            continue
        objects[stem].metadata_files.append(Path(entry.path))

    return list(objects.values())


def iter_directories(root: Path, recursive: bool):
    """Yield root and, if requested, its subdirectories (never inside a PAX)."""
    if not recursive:
        yield root
        return
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.endswith(PAX_SUFFIX))
        yield Path(dirpath)


# --------------------------------------------------------------------------- #
# Metadata extraction
# --------------------------------------------------------------------------- #
def parse_xml(path: Path) -> etree._ElementTree:
    # remove_blank_text gives clean pretty-printing once embedded in the OPEX;
    # entity resolution and network access are disabled for safety.
    parser = etree.XMLParser(remove_blank_text=True, resolve_entities=False,
                             no_network=True)
    return etree.parse(str(path), parser)


def is_dc_record(tree: etree._ElementTree) -> bool:
    return etree.QName(tree.getroot()).namespace in DC_RECORD_NAMESPACES


def dc_values(trees: list[etree._ElementTree], local_name: str) -> list[str]:
    """Distinct, whitespace-normalised DC values in priority/document order."""
    tags = [f"{{{ns}}}{local_name}" for ns in DC_NAMESPACES]
    values: list[str] = []
    for tree in trees:
        for el in tree.iter(*tags):
            text = " ".join("".join(el.itertext()).split())
            if text and text not in values:
                values.append(text)
    return values


def select_source_id(identifiers: list[str], stem: str) -> str:
    if not identifiers:
        raise MetadataError("no DC identifier found")
    if len(identifiers) == 1:
        if identifiers[0].replace(":", "_") != stem:
            log.warning("%s: only DC identifier %r does not match the stem; "
                        "using it anyway", stem, identifiers[0])
        return identifiers[0]

    matches = [i for i in identifiers if i.replace(":", "_") == stem]
    if len(matches) > 1:
        # e.g. both 'pitt:123' and 'pitt_123' present: the rule refers to the
        # identifier containing the colon.
        matches = [i for i in matches if ":" in i] or matches
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise MetadataError(f"none of the DC identifiers {identifiers} match "
                            f"the stem when ':' is replaced by '_'")
    raise MetadataError(f"several DC identifiers match the stem: {matches}")


def select_title(titles: list[str], stem: str) -> str:
    if not titles:
        raise MetadataError("no DC title found")
    if len(titles) > 1:
        log.warning("%s: several DC titles found, using the first: %r",
                    stem, titles[0])
    return titles[0]


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def write_opex(target: Path, general, fs_content, *, is_dir: bool,
               is_pax: bool) -> None:
    """Write via a temporary file so an interrupted run never leaves a
    truncated OPEX behind (which a re-run would otherwise treat as done)."""
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        Writer(str(tmp), is_dir=is_dir, is_pax=is_pax).write(general, fs_content)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def process_object(obj: PaxObject, algs: list[HashAlgorithm], *,
                   overwrite: bool, dry_run: bool, stats: Stats) -> None:
    pax_target = opex_path_for(obj.pax_dir)
    md_targets = {p: opex_path_for(p) for p in obj.metadata_files}

    def needed(target: Path) -> bool:
        if overwrite or not target.exists():
            return True
        log.debug("exists, skipping %s", target)
        stats.existing += 1
        return False

    write_pax = needed(pax_target)
    md_to_write = [p for p, t in md_targets.items() if needed(t)]
    if not write_pax and not md_to_write:
        return

    if not obj.metadata_files:
        raise MetadataError("no related metadata files found")

    trees = {p: parse_xml(p) for p in obj.metadata_files}
    dc_order = sorted(obj.metadata_files,
                      key=lambda p: (not is_dc_record(trees[p]), p.name))
    dc_trees = [trees[p] for p in dc_order]
    dc_title = select_title(dc_values(dc_trees, "title"), obj.stem)
    # Resolved before anything is written so a bad object writes nothing.
    source_id = (select_source_id(dc_values(dc_trees, "identifier"), obj.stem)
                 if write_pax else None)

    for path in md_to_write:
        target = md_targets[path]
        name = path.stem  # filename less the extension
        log.info("%s %s", "would create" if dry_run else "creating", target)
        if not dry_run:
            general = OpexMetadataContent(
                title=name,
                description=dc_title,
                source_id=name,
                identifiers=None,  # None omits the element; [] writes an empty one
            )
            fs_content = OpexFileContent.from_fs(str(path), algs,
                                                 SECURITY_DESCRIPTOR)
            write_opex(target, general, fs_content, is_dir=False, is_pax=False)
        stats.created += 1

    if write_pax:
        log.info("%s %s (SourceID=%s)", "would create" if dry_run else "creating",
                 pax_target, source_id)
        if not dry_run:
            general = OpexMetadataContent(
                title=obj.stem,
                description=dc_title,
                source_id=source_id,
                identifiers=None,
            )
            for path in obj.metadata_files:  # name order: deterministic output
                general.append_descriptive_metadata(trees[path])
            fs_content = OpexPaxContent.from_fs(str(obj.pax_dir), algs,
                                                SECURITY_DESCRIPTOR)
            write_opex(pax_target, general, fs_content, is_dir=True, is_pax=True)
        stats.created += 1


def run(root: Path, algs: list[HashAlgorithm], *, recursive: bool,
        overwrite: bool, dry_run: bool) -> Stats:
    stats = Stats()
    for directory in iter_directories(root, recursive):
        for obj in discover(directory):
            try:
                process_object(obj, algs, overwrite=overwrite, dry_run=dry_run,
                               stats=stats)
            except (MetadataError, etree.XMLSyntaxError, OSError) as exc:
                log.error("%s: skipped, %s", obj.pax_dir, exc)
                stats.failed_objects.append(str(obj.pax_dir))
    return stats


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create missing OPEX files for PAX directories and their "
                    "related XML metadata files.")
    parser.add_argument("directory", type=Path,
                        help="directory containing the PAX objects")
    parser.add_argument("--fixity", action="append", dest="fixities",
                        choices=[a.value for a in HashAlgorithm], metavar="ALG",
                        help="fixity algorithm, repeatable "
                             f"({', '.join(a.value for a in HashAlgorithm)}); "
                             "default SHA-256")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="also process subdirectories (PAX contents are "
                             "never descended into)")
    parser.add_argument("--overwrite", action="store_true",
                        help="regenerate OPEX files that already exist")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="report what would be created without writing")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="-v for progress, -vv for debug output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=(logging.WARNING, logging.INFO, logging.DEBUG)[min(args.verbose, 2)],
        format="%(levelname)s: %(message)s")

    if not args.directory.is_dir():
        log.error("not a directory: %s", args.directory)
        return 2

    algs = [HashAlgorithm(a) for a in dict.fromkeys(args.fixities or ["SHA-256"])]
    stats = run(args.directory, algs, recursive=args.recursive,
                overwrite=args.overwrite, dry_run=args.dry_run)

    print(f"{'would create' if args.dry_run else 'created'}: {stats.created}, "
          f"already present: {stats.existing}, "
          f"failed objects: {len(stats.failed_objects)}", file=sys.stderr)
    return 1 if stats.failed_objects else 0


if __name__ == "__main__":
    sys.exit(main())