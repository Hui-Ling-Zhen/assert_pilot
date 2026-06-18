# Copyright (c) 2025, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

from types import SimpleNamespace
from pathlib import Path
from utils import get_user, get_host
from collections import OrderedDict

task = 'gen_plan'
# task = 'build_KG'
# task = 'use_KG'


if task == 'gen_plan':

    subtask = 'actual_gen'
    # subtask = 'parse_result'

    if subtask == 'actual_gen':

        DEBUG = False
        # DEBUG = True

        # design_name = 'apb'
        # design_name = 'ethmac'
        # design_name = 'openMSP430'
        # design_name = 'tiny_pairing'
        design_name = 'uart'
        # design_name = 'sockit'

        if design_name == 'apb':

            file_path = (
                '/<path>/<to>/apb/apbi2c_spec.pdf'
            )
            design_dir = '/<path>/<to>/communication_controller_apb_to_i2c/apb/'
            KG_path = '/<path>/<to>/apb/graph_rag/output/20240805-165534/artifacts/clustered_graph.0.graphml'

            # KG_path = '/<path>/<to>/apb/graph_rag_apb/output/20240920-164702/artifacts/clustered_graph.graphml' # baseline KG

        elif design_name == 'ethmac':

            file_path = [
                '/<path>/<to>/ethmac/doc/eth_design_document.pdf',
                '/<path>/<to>/ethmac/doc/eth_speci.pdf',
                '/<path>/<to>/ethmac/doc/ethernet_datasheet_OC_head.pdf',
                '/<path>/<to>/ethmac/doc/ethernet_product_brief_OC_head.pdf',
            ]
            design_dir = '/<path>/<to>/communication_controller_100_mb-s_ethernet_mac_layer_switch/eth_transmitcontrol'
            KG_path = '/<path>/<to>/ethmac/doc/graph_rag/output/20240905-182504/artifacts/clustered_graph.0.graphml'

            # KG_path = '/<path>/<to>/ethmac/doc/graph_rag_ethmac/output/20240920-144204/artifacts/clustered_graph.0.graphml' # vanilla/baseline

        elif design_name == 'openMSP430':

            file_path = '/<path>/<to>/AssertLLM/spec/openMSP430.pdf'
            design_dir = '/<path>/<to>/AssertLLM/rtl/openMSP430'
            KG_path = '/<path>/<to>/AssertLLM/spec/graph_rag_openMSP430/output/20240917-111039/artifacts/clustered_graph.0.graphml'

            # KG_path = '/<path>/<to>/AssertLLM/spec/graph_rag_openMSP430/output/20240920-120728/artifacts/clustered_graph.0.graphml'  # vanilla/baseline

        elif design_name == 'tiny_pairing':

            file_path = '/<path>/<to>/AssertLLM/spec/tiny_pairing.pdf'
            design_dir = '/<path>/<to>/AssertLLM/rtl/ting_pairing'
            KG_path = '/<path>/<to>/AssertLLM/spec/graph_rag/output/20240917-090624/artifacts/clustered_graph.0.graphml'

            # KG_path = '/<path>/<to>/AssertLLM/spec/graph_rag_tiny_pairing/output/20240920-145022/artifacts/clustered_graph.0.graphml' # vanilla/baseline

        elif design_name == 'uart':

            file_path = '/<path>/<to>/AssertLLM/spec/uart.pdf'
            design_dir = (
                '/<path>/<to>/AssertLLM/rtl/uart'
            )
            KG_path = '/<path>/<to>/AssertLLM/spec/graph_rag_uart/output/20240917-111426/artifacts/clustered_graph.0.graphml'

            # KG_path = '/<path>/<to>/AssertLLM/spec/graph_rag_uart/output/20240920-163242/artifacts/clustered_graph.graphml'  # vanilla/baseline

        elif design_name == 'sockit':

            file_path = '/<path>/<to>/AssertLLM/spec/sockit.pdf'
            design_dir = (
                '/<path>/<to>/AssertLLM/rtl/sockit'
            )

            # KG_path = '/<path>/<to>/AssertLLM/spec/graph_rag_sockit/output/20250305-132436/artifacts/clustered_graph.0.graphml'
 
            KG_path = '/<path>/<to>/AssertLLM/spec/graph_rag_sockit/output/20250305-131057/artifacts/clustered_graph.0.graphml'  # vanilla/baseline

        else:
            assert False

        llm_engine_type = '<llm_engine_type>'

        # llm_model = "mistral" # ollama
        # llm_model = 'mixtral_8x7b'
        # llm_model = 'gpt-35-turbo'
        # llm_model = 'gpt-4'
        # llm_model = 'gpt-4-turbo'
        llm_model = 'gpt-4o'

        llm_args = {}


        max_tokens_per_prompt = 8000

        # use_KG = False  # baseline
        use_KG = True

        # prompt_builder = 'static'
        prompt_builder = 'dynamic'

        # if not use_KG:
        #     prompt_builder = 'static'

        if prompt_builder == 'dynamic':

            # Context enhancement flags
            enable_context_enhancement = False
            # enable_context_enhancement = True  # global summary stuff

            max_num_signals_process = float('inf')
            if DEBUG:
                max_num_signals_process = 1  # even quicker
                # max_num_signals_process = 3  # quick

            # max_prompts_per_signal = 1
            # max_prompts_per_signal = 2
            max_prompts_per_signal = 3
            if DEBUG:
                max_prompts_per_signal = 2

            # doc_retriever = False
            doc_retriever = True

            # kg_retriever = False
            kg_retriever = True

            if not use_KG:
                kg_retriever = False

            if doc_retriever:
                chunk_size = 100
                overlap = 20
                doc_k = 3

            if kg_retriever:

                # Dynamic prompt builder settings (only used if prompt_builder == 'dynamic')
                dynamic_prompt_settings = {
                    # Enable/disable different context generators
                    'rag': {
                        # 'enabled': False,
                        'enabled': True,
                        'baseline_full_spec_RTL': False,
                        # 'baseline_full_spec_RTL': True, # be careful!!!!! super simple baseline\
                        'chunk_sizes': [
                            50,
                            100,
                            200,
                            800,
                            3200,
                        ],  # Different chunk sizes to try
                        'overlap_ratios': [0.2, 0.4],  # Different overlap ratios
                        'k': 20,  # Number of chunks to retrieve per configuration
                        # 'enable_rtl': False,  # New option to enable RTL code RAG
                        'enable_rtl': True,
                    },
                    'path_based': {
                        # 'enabled': False,
                        'enabled': True,
                        'max_depth': 5,  # Maximum path length to explore
                        'representation_style': 'standard',  # Options: 'concise', 'standard', 'detailed', 'verification_focused'
                        # 'representation_style': 'detailed'
                    },
                    # slow
                    'motif': {
                        'enabled': False,
                        # 'enabled': True,
                        'patterns': {'handshake': True, 'pipeline': True, 'star': True},
                        'min_star_degree': 3,  # Minimum connections for star pattern
                        'max_motifs_per_type': 2,  # Maximum number of motifs to include per type
                    },
                    'community': {
                        'enabled': False,
                        # 'enabled': True,
                        'max_communities': 20,  # Maximum number of communities to include
                        'min_community_size': 3,  # Minimum size of communities to consider
                    },
                    'local_expansion': {
                        'enabled': False,
                        # 'enabled': True,  # Enable the local expansion generator
                        'max_depth': 2,  # Maximum BFS depth
                        'max_subgraph_size': 20,  # Maximum number of nodes to include in subgraph
                        'min_subgraph_size': 5,  # Minimum subgraph size to be considered useful
                    },
                    'guided_random_walk': {
                        'enabled': False,
                        # 'enabled': True,  # Enable the guided random walk generator
                        'num_walks': 70,  # Number of random walks to perform per focus node
                        'walk_budget': 100,  # Maximum steps per random walk
                        'teleport_probability': 0.1,  # Probability of teleporting to a gateway node
                        'local_importance_weight': 0.3,  # Weight for local node importance (alpha)
                        'direction_weight': 0.5,  # Weight for direction toward targets (beta)
                        'discovery_weight': 0.2,  # Weight for exploring new areas (gamma)
                        'max_targets_per_walk': 10,  # Maximum number of target signals per walk
                        'max_contexts_per_signal': 50,  # Maximum number of contexts to generate per signal
                    },
                    # LLM-based pruning settings
                    'pruning': {
                        # 'enabled': False, # all
                        'enabled': True,
                        'use_llm_pruning': True,
                        # 'max_contexts_per_type': 10,
                        'max_contexts_per_type': 50,
                        'max_total_contexts': 100,
                        'min_similarity_threshold': 0.3,  # Only used for fallback
                    },
                }

                # Base retrieval settings (used by multiple generators)
                kg_k = 3

                # traversal_max_depth = 0
                traversal_max_depth = 1
                # traversal_max_depth = 3

                # retrieve_edge = False
                retrieve_edge = True

        if use_KG:

            # refine_with_rtl = False
            refine_with_rtl = True

        # gen_plan_sva_using_valid_signals = False
        gen_plan_sva_using_valid_signals = True

        if gen_plan_sva_using_valid_signals:
            # valid_signals = None
            valid_signals = ['baud_clk', 'baud_freq']

        # generate_SVAs = True
        generate_SVAs = False

        verification_backend = "jasper"  # "jasper" | "verilator" | "both"

        verilator_bin = "/Users/huilingzhen/Desktop/0002-personal-projects/verification/SVA-checker/verilator/install/bin/verilator"
        verilator_top_module = None
        verilator_testbench_path = None
        verilator_build_dir = "verilator_build"
        verilator_timeout_sec = 300
        verilator_extra_args = ["--assert", "--trace"]

    elif subtask == 'parse_result':

        load_dir = f'/<path>/<to>/src/logs/'

    else:
        assert False


elif task == 'build_KG':

    # design_name = 'apb'
    # design_name = 'ethmac'
    # design_name = 'openMSP430'
    # design_name = 'tiny_pairing'
    # design_name = 'uart'
    design_name = 'sockit'

    if design_name == 'apb':

        input_file_path = (
            '/<path>/<to>/apb/apbi2c_spec.pdf'
        )

    elif design_name == 'ethmac':
        input_file_path = [
            '/<path>/<to>/ethmac/doc/eth_design_document.pdf',
            '/<path>/<to>/ethmac/doc/eth_speci.pdf',
            '/<path>/<to>/ethmac/doc/ethernet_datasheet_OC_head.pdf',
            '/<path>/<to>/ethmac/doc/ethernet_product_brief_OC_head.pdf',
        ]

    elif design_name == 'tiny_pairing':
        input_file_path = '/<path>/<to>/AssertLLM/spec/tiny_pairing.pdf'

    elif design_name == 'openMSP430':
        input_file_path = '/<path>/<to>/AssertLLM/spec/openMSP430.pdf'

    elif design_name == 'uart':
        input_file_path = (
            '/<path>/<to>/AssertLLM/spec/uart.pdf'
        )
    elif design_name == 'sockit':
        input_file_path = (
            '/<path>/<to>/AssertLLM/spec/sockit.pdf'
        )

    else:
        assert False

    env_source_path = '/<path>/<to>/rag_apb/.env'

    settings_source_path = (
        '/<path>/<to>/rag_apb/settings.yaml'
    )

    # entity_extraction_prompt_source_path = '/<path>/<to>/rag_apb/prompts/entity_extraction_vanilla_graphRAG.txt'  # original/baseline
    entity_extraction_prompt_source_path = f'/<path>/<to>/rag_apb/prompts/entity_extraction.txt'  # better- customized for HW

elif task == 'use_KG':
    KG_root = f'/<path>/<to>/data/apb/graph_rag/output/20240813-163015/artifacts'
    graphrag_method = 'local'
    query = f'What does PREADY mean?'

else:
    assert False

graphrag_local_dir = '/<path>/<to>/graphrag'  # point to your local GraphRAG repo


user = get_user()
hostname = get_host()

###################################### Below: no need to touch ######################################

# Define the root path (adjust this if necessary)
ROOT = (
    Path(__file__).resolve().parents[2]
)  # Adjust this number based on actual .git location

try:
    import git
except Exception as e:
    raise type(e)(f'{e}\nRun pip install gitpython or\nconda install gitpython')
try:
    repo = git.Repo(ROOT)
    repo_name = repo.remotes.origin.url.split('.git')[0].split('/')[-1]
    local_branch_name = repo.active_branch.name
    commit_sha = repo.head.object.hexsha
except git.exc.InvalidGitRepositoryError as e:
    raise Exception(f"Invalid Git repository at {ROOT}") from e

proj_dir = ROOT

vars = OrderedDict(vars())
FLAGS = OrderedDict()
for k, v in vars.items():
    if not k.startswith('__') and type(v) in [
        int,
        float,
        str,
        list,
        dict,
        type(None),
        bool,
    ]:
        FLAGS[k] = v
FLAGS = SimpleNamespace(**FLAGS)
