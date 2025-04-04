import json
import logging
import os
from collections import Counter, OrderedDict, defaultdict
from typing import Dict, List, Optional, Set

from core.alo_collections import AloCollection
from core.clusters import Cluster, ClusterCollection
from core.logic import (
    add_taxid_attributes,
    parse_attributes_from_config_data,
    parse_fasta_dir,
    parse_go_mapping,
    parse_ipr_mapping,
    parse_pfam_mapping,
    parse_tree_from_file,
)
from core.proteins import Protein, ProteinCollection
from core.utils import progress, yield_file_lines

logger = logging.getLogger("kinfin_logger")


def get_singletons(
    proteinCollection: ProteinCollection,
    cluster_list: List[Cluster],
) -> int:
    """
    Identify and create singleton clusters for unclustered proteins in a protein collection.

    Args:
    - proteinCollection (ProteinCollection): An instance of ProteinCollection class.
    - cluster_list (List[Cluster]): A list to which new singleton Cluster objects will be appended.

    Returns:
    - int: Number of singleton clusters created and appended to cluster_list.

    This function iterates through proteins in the given protein collection that are not yet clustered.
    For each unclustered protein, it creates a new singleton cluster and appends it to cluster_list.
    """
    logger.info("[STATUS] - Inferring singletons ...")
    singleton_idx = 0
    for protein in proteinCollection.proteins_list:
        if protein.clustered is False:
            cluster_id = f"singleton_{singleton_idx}"
            cluster = Cluster(
                cluster_id,
                [protein.protein_id],
                proteinCollection,
            )
            cluster_list.append(cluster)
            singleton_idx += 1
    return singleton_idx


def parse_cluster_file(
    output_dir: str,
    cluster_f: str,
    proteinCollection: ProteinCollection,
    available_proteomes: Set[str],
) -> List[Cluster]:
    """
    Parses a cluster file to create Cluster objects and updates protein information.
    Saves the filtered clustering data and stats to files.

    Args:
        output_dir (str): Base directory path for saving files.
        cluster_f (str): Path to the cluster file.
        proteinCollection (ProteinCollection): Collection of Protein objects.
        available_proteomes (Set[str]): Set of all available proteomes.

    Returns:
        Tuple[List[Cluster], Dict[str, any]]: List of Cluster objects and stats.

    Raises:
        FileNotFoundError: If the cluster file `cluster_f` does not exist.
    """
    cluster_list: List[Cluster] = []
    stats = {
        "total_clusters": 0,
        "total_proteins": 0,
        "total_proteomes": len(available_proteomes),
        "filtered_clusters": 0,
        "filtered_proteins": 0,
        "included_proteins": [],
        "excluded_proteins": [],
        "included_proteomes": defaultdict(int),
        "excluded_proteomes": defaultdict(int),
    }

    output_filtered_file = os.path.join(output_dir, "orthogroups.filtered.txt")
    stats_file = os.path.join(output_dir, "summary.json")

    logger.info(f"[STATUS] - Available proteomes: {available_proteomes}")

    try:

        with open(cluster_f) as fh, open(output_filtered_file, "w") as ofh:
            for line in fh:
                stats["total_clusters"] += 1
                temp: List[str] = line.rstrip("\n").split(" ")
                cluster_id, protein_ids = temp[0].replace(":", ""), temp[1:]
                protein_ids = [protein_id for protein_id in protein_ids if protein_id]

                filtered_protein_ids = []
                for protein_id in protein_ids:
                    proteome_id = protein_id.split(".")[0]  # Extract proteome ID
                    if proteome_id in available_proteomes:
                        filtered_protein_ids.append(protein_id)
                        stats["included_proteins"].append(protein_id)
                        stats["included_proteomes"][proteome_id] += 1
                    else:
                        stats["excluded_proteins"].append(protein_id)
                        stats["excluded_proteomes"][proteome_id] += 1

                stats["total_proteins"] += len(protein_ids)
                stats["filtered_proteins"] += len(filtered_protein_ids)

                if filtered_protein_ids:
                    # Only create a cluster if there are proteins left after filtering
                    cluster = Cluster(
                        cluster_id, filtered_protein_ids, proteinCollection
                    )
                    for protein_id in filtered_protein_ids:
                        protein = proteinCollection.proteins_by_protein_id[protein_id]
                        protein.clustered = True
                    cluster_list.append(cluster)
                    filtered_protein_ids.sort()
                    ofh.write(f"{cluster_id}: {', '.join(filtered_protein_ids)}\n")
                    stats["filtered_clusters"] += 1
    except Exception as e:
        logger.error("[ERROR] - Something has gone wrong in build.py")
        logger.error(f"[ERROR] - Error parsing cluster file: {e}")
        raise e

    stats["included_proteins_count"] = len(set(stats["included_proteins"]))
    stats["excluded_proteins_count"] = len(set(stats["excluded_proteins"]))

    # Convert proteome counts to lists of counts for JSON serialization
    stats["included_proteomes"] = dict(stats["included_proteomes"])
    stats["excluded_proteomes"] = dict(stats["excluded_proteomes"])

    # Reorder stats
    ordered_stats = OrderedDict(
        [
            ("total_clusters", stats["total_clusters"]),
            ("total_proteins", stats["total_proteins"]),
            ("total_proteomes", stats["total_proteomes"]),
            ("filtered_clusters", stats["filtered_clusters"]),
            ("filtered_proteins", stats["filtered_proteins"]),
            ("included_proteins_count", stats["included_proteins_count"]),
            ("excluded_proteins_count", stats["excluded_proteins_count"]),
            ("included_proteomes", stats["included_proteomes"]),
            ("excluded_proteomes", stats["excluded_proteomes"]),
            ("included_proteins", stats["included_proteins"]),
            ("excluded_proteins", stats["excluded_proteins"]),
        ]
    )

    with open(stats_file, "w") as mf:
        json.dump(
            ordered_stats,
            mf,
            separators=(", ", ": "),
            indent=4,
        )

    return cluster_list


# cli
def parse_domains_from_functional_annotations_file(
    functional_annotation_f: str,
    proteinCollection: ProteinCollection,
) -> None:
    """
    Parse functional annotations from a file and populate ProteinCollection with parsed data.

    Parameters:
    - functional_annotation_f (str): Path to the functional annotation file.
    - proteinCollection (ProteinCollection): Instance of ProteinCollection class to store parsed data.
    - pfam_mapping (bool): Flag indicating whether to parse Pfam mappings.
    - ipr_mapping (bool): Flag indicating whether to parse InterPro mappings.
    - pfam_mapping_f (str): File path to the Pfam mapping file.
    - ipr_mapping_f (str): File path to the InterPro mapping file.
    - go_mapping_f (str): File path to the GO mapping file.

    Raises:
    - ValueError: If the functional annotation file lacks a header.

    Notes:
    - The function reads each line of the functional annotation file, parses relevant data,
      and populates the proteinCollection with domain annotations and GO terms.
    - It also optionally parses additional mappings (Pfam, InterPro, GO) based on provided flags.
    - Updates proteinCollection.functional_annotation_parsed and proteinCollection.domain_desc_by_id_by_source.
    """

    logger.info(
        f"[STATUS] - Parsing {functional_annotation_f} ... this may take a while"
    )

    for line in yield_file_lines(functional_annotation_f):
        temp: List[str] = line.split()
        if temp[0].startswith("#"):
            proteinCollection.domain_sources = temp[1:]

        else:
            if not proteinCollection.domain_sources:
                error_msg = f"[ERROR] - {functional_annotation_f} does not seem to have a header."
                raise ValueError(error_msg)

            domain_protein_id: str = temp.pop(0)
            go_terms: List[str] = []
            domain_counter_by_domain_source: Dict[str, Counter[str]] = {}
            for idx, field in enumerate(temp):
                if field != "None":
                    domain_source: str = proteinCollection.domain_sources[idx]
                    domain_string: List[str] = field.split(";")
                    domain_counts_by_domain_id: Dict[str, int] = {}
                    for domain_id_count in domain_string:
                        domain_id: str
                        domain_count: int = 1
                        if domain_source == "GO":
                            domain_id = domain_id_count
                        else:
                            domain_id, domain_count_str = domain_id_count.rsplit(":", 2)
                            domain_count = int(domain_count_str)
                        domain_counts_by_domain_id[domain_id] = domain_count
                    domain_counter: Counter[str] = Counter(domain_counts_by_domain_id)
                    domain_counter_by_domain_source[domain_source] = domain_counter
            proteinCollection.add_annotation_to_protein(
                domain_protein_id=domain_protein_id,
                domain_counter_by_domain_source=domain_counter_by_domain_source,
                go_terms=go_terms,
            )

    proteinCollection.functional_annotation_parsed = True


# common
def build_AloCollection(
    config_f: str,
    nodesdb_f: str,
    taxranks: List[str],
    tree_f: Optional[str],
    taxon_idx_mapping_file: Optional[str],
) -> AloCollection:
    """
    Builds an AloCollection object from command-line interface (CLI) inputs.

    Args:
        config_f (str): Path to the configuration file containing proteome attributes.
        nodesdb_f (str): Path to the nodes database file for inferring taxonomic ranks.
        taxranks (List[str]): List of taxonomic ranks to be inferred.
        tree_f (Optional[str]): Path to the tree file. If provided, ALOs are added from the tree.

    Returns:
        AloCollection: An instance of the AloCollection class containing parsed data.
    """
    (
        proteomes,
        proteome_id_by_species_id,
        attributes,
        level_by_attribute_by_proteome_id,
    ) = parse_attributes_from_config_data(config_f, taxon_idx_mapping_file)
    # Add taxonomy if needed
    if "TAXID" in set(attributes):
        logger.info(
            "[STATUS] - Attribute 'TAXID' found, inferring taxonomic ranks from nodesDB"
        )
        attributes, level_by_attribute_by_proteome_id = add_taxid_attributes(
            attributes=attributes,
            level_by_attribute_by_proteome_id=level_by_attribute_by_proteome_id,
            nodesdb_f=nodesdb_f,
            taxranks=taxranks,
        )

    # Add ALOs from tree if provided
    tree_ete, node_idx_by_proteome_ids = parse_tree_from_file(
        tree_f,
        attributes,
        level_by_attribute_by_proteome_id,
        proteomes,
    )

    logger.info("[STATUS] - Building AloCollection ...")
    return AloCollection(
        proteomes=proteomes,
        attributes=attributes,
        proteome_id_by_species_id=proteome_id_by_species_id,
        level_by_attribute_by_proteome_id=level_by_attribute_by_proteome_id,
        node_idx_by_proteome_ids=node_idx_by_proteome_ids,
        tree_ete=tree_ete,
    )


def get_protein_list_from_seq_f(sequence_ids_f: str, aloCollection: AloCollection):
    logger.info(f"[STATUS] - Parsing sequence IDs: {sequence_ids_f} ...")

    proteins_list: List[Protein] = []
    for line in yield_file_lines(sequence_ids_f):
        temp = line.split(": ")
        sequence_id = temp[0]
        protein_id = (
            temp[1]
            .split(" ")[0]
            .replace(":", "_")
            .replace(",", "_")
            .replace("(", "_")
            .replace(")", "_")
        )  # orthofinder replaces characters
        species_id = sequence_id.split("_")[0]
        if proteome_id := aloCollection.proteome_id_by_species_id.get(species_id, None):
            protein = Protein(protein_id, proteome_id, species_id, sequence_id)
            proteins_list.append(protein)
    return proteins_list


# common
def build_ProteinCollection(
    sequence_ids_f: str,
    aloCollection: AloCollection,
    fasta_dir: Optional[str],
    species_ids_f: Optional[str],
    functional_annotation_f: Optional[str],
    pfam_mapping: bool,
    ipr_mapping: bool,
    pfam_mapping_f: str,
    go_mapping_f: str,
    ipr_mapping_f: str,
) -> ProteinCollection:
    proteins_list = get_protein_list_from_seq_f(
        sequence_ids_f=sequence_ids_f,
        aloCollection=aloCollection,
    )
    proteinCollection = ProteinCollection(proteins_list)

    logger.info(f"[STATUS]\t - Proteins found = {proteinCollection.protein_count}")

    if fasta_dir is not None and species_ids_f is not None:
        fasta_len_by_protein_id = parse_fasta_dir(
            fasta_dir=fasta_dir,
            species_ids_f=species_ids_f,
        )
        logger.info("[STATUS] - Adding FASTAs to ProteinCollection ...")
        parse_steps: float = proteinCollection.protein_count / 100
        for idx, protein in enumerate(proteinCollection.proteins_list):
            protein.update_length(fasta_len_by_protein_id[protein.protein_id])
            progress(idx + 1, parse_steps, proteinCollection.protein_count)
        aloCollection.fastas_parsed = True
        proteinCollection.fastas_parsed = True
    else:
        logger.info(
            "[STATUS] - No Fasta-Dir given, no AA-span information will be reported ..."
        )

    if functional_annotation_f is not None:
        parse_domains_from_functional_annotations_file(
            functional_annotation_f=functional_annotation_f,
            proteinCollection=proteinCollection,
        )
        domain_desc_by_id_by_source = {}

        if pfam_mapping and "Pfam" in proteinCollection.domain_sources:
            domain_desc_by_id_by_source["Pfam"] = parse_pfam_mapping(pfam_mapping_f)

        if ipr_mapping and "IPR" in proteinCollection.domain_sources:
            domain_desc_by_id_by_source["IPR"] = parse_ipr_mapping(ipr_mapping_f)

        if go_mapping_f:
            domain_desc_by_id_by_source["GO"] = parse_go_mapping(go_mapping_f)

        proteinCollection.domain_desc_by_id_by_source = domain_desc_by_id_by_source

    return proteinCollection


def build_ClusterCollection(
    output_dir: str,
    cluster_f: str,
    proteinCollection: ProteinCollection,
    infer_singletons: Optional[bool],
    available_proteomes: Set[str],
) -> ClusterCollection:
    logger.info(f"[STATUS] - Parsing {cluster_f} ... this may take a while")
    cluster_list: List[Cluster] = parse_cluster_file(
        output_dir,
        cluster_f,
        proteinCollection,
        available_proteomes,
    )

    inferred_singletons_count = 0
    if infer_singletons:
        inferred_singletons_count = get_singletons(proteinCollection, cluster_list)

    return ClusterCollection(
        cluster_list,
        inferred_singletons_count,
        proteinCollection.functional_annotation_parsed,
        proteinCollection.fastas_parsed,
        proteinCollection.domain_sources,
    )
