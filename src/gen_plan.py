# Copyright (c) 2025, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

from sva_extraction import extract_svas_from_block
from doc_KG_processor import create_context_generators
from dynamic_prompt_builder import DynamicPromptBuilder
from load_result import load_svas, load_nl_plans, load_jasper_reports, load_pdf_stats
from rtl_parsing import refine_kg_from_rtl
from verilator_backend import run_verilator_flow
from utils_gen_plan import (
    extract_proof_status,
    analyze_coverage_of_proven_svas,
    count_tokens_in_file,
    find_original_tcl_file,
)
from design_context_summarizer import DesignContextSummarizer
import os, math
import subprocess
from config import FLAGS
from saver import saver
from utils import OurTimer
from utils_LLM import get_llm, llm_inference
import networkx as nx
from typing import Tuple, List, Dict, Optional, Set, Union
from PyPDF2 import PdfReader
import json
import random
import re
import pandas as pd
from tabulate import tabulate
from pathlib import Path
from tqdm import tqdm

print = saver.log_info


def gen_plan():
    """
    Main function to generate test plans and SVAs from a design specification,
    and run the configured verification backend(s).
    """
    timer = OurTimer()

    # Initialize context_summarizer as None
    context_summarizer = None

    if FLAGS.subtask == 'actual_gen':
        print("Starting the test plan generation process...")

        timer.start_timing()
        print("Step 1: Reading the PDF file(s)...")
        file_path = FLAGS.file_path
        spec_text, pdf_stats = read_pdf(file_path)
        timer.time_and_clear("Read PDF")

        kg_nx, kg_json, rtl_knowledge = None, None, None
        if FLAGS.use_KG:
            print("Step 2: Loading and processing the Knowledge Graph...")
            kg_nx, kg_json = load_and_process_kg(FLAGS.KG_path)
            timer.time_and_clear("Load and process KG")

            if FLAGS.refine_with_rtl:
                print("Step 2b: Refining Knowledge Graph with RTL information...")
                kg_nx, rtl_knowledge = refine_kg_from_rtl(kg_nx)
                print(
                    f"Refined Knowledge Graph now has {len(kg_nx.nodes())} nodes and {len(kg_nx.edges())} edges"
                )
                # Update the JSON representation after refinement
                kg_json = convert_nx_to_json(kg_nx)
                timer.time_and_clear("Refine KG with RTL")

        print("Step 3: Initializing the language model...")
        llm_agent = get_llm(model_name=FLAGS.llm_model, **FLAGS.llm_args)
        timer.time_and_clear("Initialize LLM")

        print("Step 4: Extracting valid signal names...")
        if FLAGS.valid_signals is None:
            _, valid_signals = write_svas_to_file(
                []
            )  # We pass an empty list just to extract valid signals
        else:
            valid_signals = FLAGS.valid_signals    
        print(f"Valid signal names: {', '.join(sorted(valid_signals))}")
        timer.time_and_clear("Extract valid signals")

        # Initialize context enhancement if enabled - do this after we have valid_signals
        if FLAGS.enable_context_enhancement:
            print("Step 4b: Initializing Design Context Summarizer...")

            # Initialize the context summarizer once
            context_summarizer = DesignContextSummarizer(llm_agent=llm_agent)

            # Extract RTL text from rtl_knowledge if available - simplified approach
            rtl_text = (
                rtl_knowledge['combined_content']
                if rtl_knowledge is not None and 'combined_content' in rtl_knowledge
                else ""
            )

            # Generate the global summary once
            context_summarizer.generate_global_summary(
                spec_text, rtl_text, list(valid_signals)
            )

            # Pre-generate summaries for all signals we'll process
            signals_to_process = sorted(valid_signals)

            if not math.isinf(FLAGS.max_num_signals_process):
                signals_to_process = signals_to_process[: FLAGS.max_num_signals_process]

            for signal_name in signals_to_process:
                # Get signal-specific RTL if available
                signal_rtl = (
                    rtl_knowledge.get(signal_name, "")
                    if isinstance(rtl_knowledge, dict)
                    else ""
                )
                context_summarizer.get_signal_specific_summary(
                    signal_name, spec_text, signal_rtl
                )

            timer.time_and_clear("Initialize Context Summarizer")

        print("Step 5: Generating natural language test plans...")
        nl_plans = generate_nl_plans(
            spec_text,
            kg_json,
            llm_agent,
            valid_signals if FLAGS.gen_plan_sva_using_valid_signals else None,
            rtl_knowledge,
            context_summarizer,  # Pass the context_summarizer
        )
        with open(Path(saver.logdir) / 'nl_plans.txt', 'w') as f:
            c = 1
            for signal_name, plans in nl_plans.items():
                f.write(f'Signal {signal_name}:\n')
                for plan in plans:
                    f.write(f'Plan {c}: {plan}\n')
                    c += 1
                f.write('\n')
        timer.time_and_clear("Generate NL plans")

        if FLAGS.generate_SVAs:

            print("Step 6: Generating SVAs...")
            svas = generate_svas(
                spec_text,
                nl_plans,
                kg_json,
                llm_agent,
                valid_signals if FLAGS.gen_plan_sva_using_valid_signals else None,
                rtl_knowledge,
                context_summarizer,  # Pass the context_summarizer
            )
            if len(svas) == 0:
                raise RuntimeError(f'No SVA generated/extracted')
            timer.time_and_clear("Generate SVAs")

            # Print generated SVAs
            print("\nGenerated SVAs:")
            for i, sva in enumerate(svas, 1):
                print(f"{i}. {sva}")
            print('')  # Add a blank line for readability

            print("Step 7: Writing SVAs to files...")
            sva_file_paths, _ = write_svas_to_file(svas)
            timer.time_and_clear("Write SVAs to files")

            print("Step 8: Running verification backend(s)...")
            backend_results = run_verification_backends(svas, sva_file_paths)
            jasper_reports = backend_results.get("jasper_reports", [])
            coverage_report = backend_results.get("coverage_report", "")
            timer.time_and_clear("Run verification backend(s)")

            print("Step 9: Analyzing and printing results...")
            analyze_results(
                pdf_stats,
                nl_plans,
                svas,
                jasper_reports,
                coverage_report,
                backend_results,
            )
            timer.time_and_clear("Analyze results")

            print('Test plan generation and coverage evaluation process completed.')

    elif FLAGS.subtask == 'parse_result':
        print("Parsing results from a previous run...")
        load_dir = FLAGS.load_dir

        print("Loading PDF statistics...")
        pdf_stats = load_pdf_stats(load_dir)

        print("Loading natural language test plans...")
        nl_plans = load_nl_plans(load_dir)

        print("Loading SVAs...")
        svas = load_svas(load_dir)

        print("Loading Jasper reports...")
        jasper_reports = load_jasper_reports(load_dir)

        print("Analyzing results...")
        analyze_results(pdf_stats, nl_plans, svas, jasper_reports)

        timer.time_and_clear("parse_result")
        print('parse_result completed.')

    else:
        raise NotImplementedError()

    # Print the durations log
    timer.print_durations_log(print_func=print)


def read_pdf(file_path: Union[str, List[str]]) -> Tuple[str, dict]:
    """
    Read one or multiple PDF files and extract their content.

    Args:
        file_path (Union[str, List[str]]): Path to a single PDF file or a list of paths to multiple PDF files.

    Returns:
        Tuple[str, dict]: A tuple containing the extracted text and file statistics.
    """
    if isinstance(file_path, str):
        file_paths = [file_path]
    elif isinstance(file_path, list):
        file_paths = file_path
    else:
        raise ValueError("file_path must be a string or a list of strings")

    all_text = ""
    total_pages = 0
    total_tokens = 0
    total_file_size = 0

    for path in file_paths:
        pdf_reader = PdfReader(path)
        text = ""
        for page in pdf_reader.pages:
            text += page.extract_text()

        all_text += text + "\n\n"  # Add some separation between different PDFs
        total_pages += len(pdf_reader.pages)

        # Create a temporary file to store the extracted text
        temp_file_path = f"temp_{os.path.basename(path)}.txt"
        with open(temp_file_path, 'w', encoding='utf-8') as temp_file:
            temp_file.write(text)

        # Count tokens using the helper function
        total_tokens += count_tokens_in_file(temp_file_path)

        # Remove the temporary file
        os.remove(temp_file_path)

        total_file_size += os.path.getsize(path)

    stats = {
        "num_pages": total_pages,
        "num_tokens": total_tokens,
        "file_size": total_file_size,
        "num_files": len(file_paths),
    }

    # Print only the first few lines of spec_text
    num_lines_to_print = 5  # You can adjust this number as needed
    lines = all_text.splitlines()
    first_few_lines = '\n'.join(lines[:num_lines_to_print])
    print(
        f'First {num_lines_to_print} lines of spec_text (truncated):\n{first_few_lines}\n...'
    )
    print(f'Total number of lines in spec_text: {len(lines)}')

    return all_text.strip(), stats


def load_and_process_kg(kg_path: str) -> Tuple[nx.Graph, Dict]:
    """
    Load the Knowledge Graph from a GraphML file and process it into both a NetworkX graph
    and a JSON format suitable for prompting. Prints detailed information about the graph structure.

    Args:
        kg_path (str): Path to the GraphML file containing the Knowledge Graph.

    Returns:
        Tuple[nx.Graph, Dict]: A tuple containing the original NetworkX graph and the processed JSON format.
    """
    # Load the graph from GraphML file
    G = nx.read_graphml(kg_path)

    # Convert the graph to JSON format
    json_graph = convert_nx_to_json(G)

    # Print detailed information about the graph
    print(f"Knowledge Graph loaded from {kg_path}")
    print(f"Number of nodes: {len(G.nodes())}")
    print(f"Number of edges: {len(G.edges())}")

    # Sample and print some node attributes
    if G.nodes:
        sample_node = random.choice(list(G.nodes))
        print("\nExample node attributes:")
        for k, v in G.nodes[sample_node].items():
            print(f"  {k}: {v}")

    # Sample and print some edge attributes
    if G.edges:
        sample_edge = random.choice(list(G.edges))
        print("\nExample edge attributes:")
        for k, v in G.edges[sample_edge].items():
            print(f"  {k}: {v}")

    # Print information about attribute keys
    node_attr_keys = set().union(*(data.keys() for _, data in G.nodes(data=True)))
    edge_attr_keys = set().union(*(data.keys() for _, _, data in G.edges(data=True)))

    print("\nNode attribute keys:")
    print(", ".join(node_attr_keys))

    print("\nEdge attribute keys:")
    print(", ".join(edge_attr_keys))

    return G, json_graph


# Helper function to convert NetworkX graph to JSON format


def convert_nx_to_json(G: nx.Graph) -> Dict:
    """
    Convert a NetworkX graph to a JSON-friendly dictionary format.

    Args:
        G (nx.Graph): The NetworkX graph to convert.

    Returns:
        Dict: A dictionary representation of the graph.
    """
    json_graph = {"nodes": [], "edges": []}
    for node, data in G.nodes(data=True):
        json_graph["nodes"].append(
            {
                "id": node,
                "attributes": {k: str(v) for k, v in data.items()},
            }
        )
    for u, v, data in G.edges(data=True):
        json_graph["edges"].append(
            {
                "source": u,
                "target": v,
                "attributes": {k: str(v) for k, v in data.items()},
            }
        )
    return json_graph


def generate_nl_plans(
    spec_text: str,
    kg: Optional[Dict],
    llm_agent,
    valid_signals: Optional[Set[str]],
    rtl_knowledge,
    context_summarizer,
) -> Dict[str, List[str]]:
    """
    Generate natural language test plans using the design specification,
    optionally using a Knowledge Graph, LLM, and valid signal names.

    Args:
        spec_text (str): The design specification text.
        kg (Optional[Dict]): The processed Knowledge Graph, if available.
        llm_agent: The language model agent.
        valid_signals (Optional[Set[str]]): Set of valid signal names, if using valid signals.

    Returns:
        Dict[str, List[str]]: A dictionary mapping signal names to lists of generated natural language test plans.
    """
    if FLAGS.prompt_builder == 'dynamic':
        return generate_dynamic_nl_plans(
            spec_text, kg, llm_agent, valid_signals, rtl_knowledge, context_summarizer
        )
    elif FLAGS.prompt_builder == 'static':
        return generate_static_nl_plans(spec_text, kg, llm_agent, valid_signals)
    else:
        raise NotImplementedError("Unsupported prompt builder type")


def generate_dynamic_nl_plans(
    spec_text: str,
    kg: Optional[Dict],
    llm_agent,
    valid_signals: Optional[Set[str]],
    rtl_knowledge,
    context_summarizer,
) -> Dict[str, List[str]]:
    """
    Generate natural language test plans using dynamic context synthesis.
    """
    # Create context generators using factory function
    context_generators = create_context_generators(
        spec_text, kg, valid_signals, rtl_knowledge
    )

    # Initialize the prompt builder with context_summarizer
    prompt_builder = DynamicPromptBuilder(
        context_generators=context_generators,
        pruning_config=FLAGS.dynamic_prompt_settings['pruning'],
        llm_agent=llm_agent,
        context_summarizer=context_summarizer,  # Pass the summarizer
    )

    nl_plans = {}
    for i, signal_name in enumerate(sorted(valid_signals)):  # sorted is key!
        if i >= FLAGS.max_num_signals_process:
            print(
                f'Reached max signals limit ({FLAGS.max_num_signals_process}), stopping.'
            )
            break

        print(f'Processing signal {i+1}/{len(valid_signals)}: {signal_name}')
        query = f"{signal_name}"

        # Get dynamic contexts with enhancement integrated if enabled
        dynamic_context_list = prompt_builder.build_prompt(
            query=query,
            base_prompt="",
            signal_name=signal_name,
            enable_context_enhancement=FLAGS.enable_context_enhancement,  # Pass the enhancement flag
        )

        print(
            f'Generated {len(dynamic_context_list)} dynamic contexts for signal {signal_name}'
        )

        assert len(dynamic_context_list) <= FLAGS.max_prompts_per_signal

        all_signal_plans = []

        # Process each dynamic context separately
        for context_idx, dynamic_context in enumerate(dynamic_context_list):
            print(
                f'Processing dynamic context {context_idx+1}/{len(dynamic_context_list)} for {signal_name}'
            )

            # Context enhancement is now integrated in prompt builder
            # No need to call add_enhanced_context here anymore

            # Rest of the existing code...
            full_prompt = construct_static_nl_prompt(
                dynamic_context,
                kg=None,  # KG info already included in dynamic context
                valid_signals=valid_signals,
            )
            full_prompt += f"\n\nGenerate diverse test plans for the signal '{signal_name}'. Each test plan should be on a new line and start with 'Plan: '."

            try:
                # Get LLM response for this context prompt
                result = llm_inference(
                    llm_agent, full_prompt, f"NL_Plans_{signal_name}_{context_idx+1}"
                )

                # Extract plans from the result
                context_plans = []
                for line in result.split('\n'):
                    if line.strip().startswith('Plan:'):
                        plan = line.split(':', 1)[-1].strip()
                        context_plans.append(plan)

                all_signal_plans.extend(context_plans)
                print(
                    f"Generated {len(context_plans)} plans from context {context_idx+1} for signal {signal_name}"
                )

            except Exception as e:
                print(
                    f"Error generating NL plans for signal {signal_name} context {context_idx+1}: {str(e)}"
                )
                print(f"Continuing with other contexts for this signal")
                continue

        # Deduplicate plans
        unique_plans = []
        plan_set = set()
        for plan in all_signal_plans:
            # Use a simplified version of the plan for deduplication
            # Remove extra spaces and convert to lowercase
            simplified_plan = ' '.join(plan.lower().split())
            if simplified_plan not in plan_set:
                plan_set.add(simplified_plan)
                unique_plans.append(plan)

        nl_plans[signal_name] = unique_plans
        print(
            f"Generated {len(unique_plans)} unique plans for signal {signal_name} from {len(all_signal_plans)} total plans"
        )

    return nl_plans


def generate_static_nl_plans(
    spec_text: str, kg: Optional[Dict], llm_agent, valid_signals: Optional[Set[str]]
) -> Dict[str, List[str]]:
    nl_gen_prompt = construct_static_nl_prompt(spec_text, kg, valid_signals)

    try:
        result = llm_inference(llm_agent, nl_gen_prompt, "NL_Plans")

        # Parse the result into a dictionary
        nl_plans = parse_nl_plans(result)
        return nl_plans
    except Exception as e:
        print(f"Error generating NL description: {str(e)}")
        raise


def generate_svas(
    spec_text: str,
    nl_plans: Dict[str, List[str]],
    kg: Optional[Dict],
    llm_agent,
    valid_signals: Optional[Set[str]],
    rtl_knowledge,
    context_summarizer,
) -> List[str]:
    """
    Generate SVAs using LLM based on the design specification, natural language test plans,
    and optionally a Knowledge Graph, ensuring only valid signal names are used if provided.

    Args:
        spec_text (str): The design specification text.
        nl_plans (Dict[str, List[str]]): Dictionary mapping signal names to lists of natural language test plans.
        kg (Optional[Dict]): The processed Knowledge Graph, if available.
        llm_agent: The language model agent.
        valid_signals (Optional[Set[str]]): Set of valid signal names, if using valid signals.

    Returns:
        List[str]: A list of generated SVAs.
    """
    if FLAGS.prompt_builder == 'dynamic':
        return generate_dynamic_svas(
            spec_text,
            nl_plans,
            kg,
            llm_agent,
            valid_signals,
            rtl_knowledge,
            context_summarizer,
        )
    elif FLAGS.prompt_builder == 'static':
        return generate_static_svas(spec_text, nl_plans, kg, llm_agent, valid_signals)
    else:
        raise NotImplementedError("Unsupported prompt builder type")


def generate_dynamic_svas(
    spec_text: str,
    nl_plans: Dict[str, List[str]],
    kg: Optional[Dict],
    llm_agent,
    valid_signals: Optional[Set[str]],
    rtl_knowledge,
    context_summarizer,
) -> List[str]:
    """
    Generate SVAs using dynamic context synthesis with support for multiple contexts per signal.
    """
    # Create context generators using factory function
    context_generators = create_context_generators(
        spec_text, kg, valid_signals, rtl_knowledge
    )

    # Initialize the prompt builder with the context_summarizer
    prompt_builder = DynamicPromptBuilder(
        context_generators=context_generators,
        pruning_config=FLAGS.dynamic_prompt_settings['pruning'],
        llm_agent=llm_agent,
        context_summarizer=context_summarizer,  # Pass the summarizer here
    )

    all_svas = []
    for i, (signal_name, plans) in enumerate(nl_plans.items()):
        if i >= FLAGS.max_num_signals_process:
            print(
                f'Reached max signals limit ({FLAGS.max_num_signals_process}), stopping.'
            )
            break

        if len(plans) == 0:
            print(f'Empty NL plans for signal {signal_name}')
            continue

        print(f'Processing signal {i+1}/{len(nl_plans)}: {signal_name}')

        # Get dynamic contexts with enhancement integrated if enabled
        dynamic_context_list = prompt_builder.build_prompt(
            query=signal_name,
            base_prompt="Generate SystemVerilog Assertions based on the following information:",
            signal_name=signal_name,
            enable_context_enhancement=FLAGS.enable_context_enhancement,  # Pass the enhancement flag
        )

        print(
            f'Generated {len(dynamic_context_list)} dynamic contexts for signal {signal_name}'
        )

        # Get SVA examples once (reused for each context)
        sva_examples = get_sva_icl_examples()

        signal_svas = []

        # Determine if we should distribute plans or use all plans for each context
        distribute_plans = len(plans) > 10 and len(dynamic_context_list) > 1

        if distribute_plans:
            # Prepare distributed plans
            plans_per_context = [[] for _ in range(len(dynamic_context_list))]
            for j, plan in enumerate(plans):
                context_idx = j % len(dynamic_context_list)
                plans_per_context[context_idx].append((j, plan))

            print(
                f'Distributing {len(plans)} plans across {len(dynamic_context_list)} contexts'
            )
        else:
            # Use all plans for each context
            plans_text = "\n".join(
                f"Plan {j+1}: {plan}" for j, plan in enumerate(plans)
            )

        # Process each dynamic context
        for context_idx, dynamic_context in enumerate(dynamic_context_list):
            # Context enhancement is now integrated within the prompt builder
            # No need to call add_enhanced_context here anymore

            try:
                # Determine which plans to use for this context
                if distribute_plans:
                    context_plans = plans_per_context[context_idx]
                    if not context_plans:
                        print(f'No plans assigned to context {context_idx+1}, skipping')
                        continue

                    # Create subset of plans text for this context
                    current_plans_text = "\n".join(
                        f"Plan {j+1}: {plan}" for j, plan in context_plans
                    )
                    print(
                        f'Processing context {context_idx+1} with {len(context_plans)} plans'
                    )
                else:
                    current_plans_text = plans_text
                    print(
                        f'Processing context {context_idx+1} with all {len(plans)} plans'
                    )

                # Create the full prompt for this context
                full_prompt = (
                    f"{dynamic_context}\n\n"
                    f"Natural Language Test Plans for signal '{signal_name}':\n{current_plans_text}\n\n"
                    f"{sva_examples}\n\n"
                    "Generate one SVA for each of the provided natural language test plans. "
                    "Enclose each SVA in triple backticks (```) and prefix it with 'SVA:'."
                )
                result = llm_inference(
                    llm_agent, full_prompt, f"SVAs_{signal_name}_{context_idx+1}"
                )

                context_svas = extract_svas_from_block(result)
                print(
                    f'Generated {len(context_svas)} SVAs from context {context_idx+1} for signal {signal_name}'
                )
                signal_svas.extend(context_svas)

            except Exception as e:
                print(
                    f"Error generating SVAs for signal {signal_name} context {context_idx+1}: {str(e)}"
                )
                print(f"Continuing with other contexts for this signal")
                continue

        # Deduplicate SVAs
        unique_svas = []
        sva_set = set()
        for sva in signal_svas:
            # Use a simplified version of the SVA for deduplication
            # Remove comments, extra spaces and convert to lowercase
            simplified_sva = ' '.join(
                line
                for line in sva.lower().split('\n')
                if not line.strip().startswith('//')
            ).strip()

            if simplified_sva not in sva_set:
                sva_set.add(simplified_sva)
                unique_svas.append(sva)

        print(
            f'Generated {len(unique_svas)} unique SVAs for signal {signal_name} from {len(signal_svas)} total SVAs'
        )
        all_svas.extend(unique_svas)

    return all_svas


def generate_static_svas(
    spec_text: str,
    nl_plans: Dict[str, List[str]],
    kg: Optional[Dict],
    llm_agent,
    valid_signals: Optional[Set[str]],
) -> List[str]:
    """
    Generate SVAs using LLM based on the design specification, natural language test plans,
    and optionally a Knowledge Graph, ensuring only valid signal names are used if provided.

    Args:
        spec_text (str): The design specification text.
        nl_plans (Dict[str, List[str]]): Dictionary mapping signal names to lists of natural language test plans.
        kg (Optional[Dict]): The processed Knowledge Graph, if available.
        llm_agent: The language model agent.
        valid_signals (Optional[Set[str]]): Set of valid signal names, if using valid signals.

    Returns:
        List[str]: A list of generated SVAs.
    """
    sva_gen_prompt = construct_static_sva_prompt(spec_text, nl_plans, kg, valid_signals)

    # try:

    result = llm_inference(llm_agent, sva_gen_prompt, "SVAs")

    # Use extract_svas_from_block to extract SVAs
    svas = extract_svas_from_block(result)

    if not svas:
        print(
            "Warning: No valid SVAs were generated. Please check the output and adjust the prompt if necessary."
        )
    else:
        print(f"Generated {len(svas)} SVAs.")

    return svas

    # except Exception as e:
    #     print(f"Error generating SVAs: {str(e)}")
    #     return []


def get_sva_icl_examples():
    return """
    Examples:
    SVA:
    ```
    @(posedge PCLK) ((PWDATA >= 230) && (PWDATA <= 255)) |-> (PWDATA >= 205) && (PWDATA <= 255);
    ```
    NL: that when PWDATA is within the range of 230 to 255, in the next cycle PWDATA will remain within the range of 205 to 255. Use the signals 'PCLK' for the clock edge and 'PWDATA' for the data being checked.

    SVA:
    ```
    @(posedge PCLK) (PRESETn) |-> (PWDATA >= 0) && (PWDATA <= 45);
    ```
    NL: that the input data is within the valid range when not in reset. Use the signals 'PRESETn', 'PCLK', and 'PWDATA'.
    """


def construct_static_nl_prompt(
    spec_text: str, kg: Optional[Dict], valid_signals: Optional[Set[str]]
) -> str:
    nl_gen_prompt = f"""
    Given the following design specification{' and Knowledge Graph' if kg else ''}, generate natural language test plans:

    {spec_text}

    """

    if valid_signals:
        nl_gen_prompt += f"""
    CRITICAL - Valid Signal Names (USE ONLY THESE SIGNALS):
    {', '.join(sorted(valid_signals))}

    WARNING: It is ABSOLUTELY ESSENTIAL that you ONLY use signals from the above list in your test plans. 
    DO NOT introduce or use ANY signals that are not in this list. Any test plan using undefined signals will be considered invalid.

    """

    if kg:
        nl_gen_prompt += f"""
    Knowledge Graph:
    {json.dumps(kg, indent=2)}

    """

    nl_gen_prompt += """
    Use the following examples as a guide for the format and style of the test plans:

    1. that when PWDATA is within the range of 230 to 255, in the next cycle PWDATA will remain within the range of 205 to 255. Use the signals 'PCLK' for the clock edge and 'PWDATA' for the data being checked.
    2. that the input data is within the valid range when not in reset. Use the signals 'PRESETn', 'PCLK', and 'PWDATA'.
    3. that if the input data 'PWDATA' is within the range of 138 to 153 inclusive, then in the subsequent cycles, 'PWDATA' must continue to be within the range of 98 to 153 inclusive. Use the signals 'PWDATA' and 'PCLK'.
    4. that the input data PWDATA has a value between 83 and 165, inclusive, 3 clock cycles after the reset signal PRESETn becomes deasserted. Use the signals 'PRESETn', 'PCLK', and 'PWDATA'.
    5. that the input data signal 'PWDATA' is within the range 0 to 45 inclusive, starting from four clock cycles after the reset signal 'PRESETn' becomes deasserted. Use the signals 'PRESETn', 'PCLK', and 'PWDATA'.

    Generate diverse test plans based on the given specification"""

    if kg:
        nl_gen_prompt += " and Knowledge Graph"

    nl_gen_prompt += "."

    nl_gen_prompt += """

    FINAL REMINDER:
    - You MUST ONLY use signals from the 'Valid Signal Names' list provided above.
    - DO NOT introduce or use any signals that are not in this list.
    - Any test plan using undefined signals will be rejected.
    - Double-check each test plan to ensure it ONLY uses valid signals.

    For each test plan, start with the signal name followed by a colon, then the test plan. For example:
    PWDATA: that when PWDATA is within the range of 230 to 255, in the next cycle PWDATA will remain within the range of 205 to 255.
    """

    return nl_gen_prompt


def construct_static_sva_prompt(
    spec_text: str,
    nl_plans: Dict[str, List[str]],
    kg: Optional[Dict],
    valid_signals: Optional[Set[str]],
) -> str:
    sva_gen_prompt = f"""
    Given the following design specification, natural language test plans{', and Knowledge Graph' if kg else ''}, generate SVAs (System Verilog Assertions):

    {spec_text}

    Test Plans:
    """

    for signal, plans in nl_plans.items():
        sva_gen_prompt += f"\n{signal}:\n"
        for i, plan in enumerate(plans, 1):
            sva_gen_prompt += f"  {i}. {plan}\n"

    if valid_signals:
        sva_gen_prompt += f"""
    Valid Signal Names:
    {', '.join(sorted(valid_signals))}

    """

    if kg:
        sva_gen_prompt += f"""
    Knowledge Graph:
    {json.dumps(kg, indent=2)}

    """

    sva_gen_prompt += """
    Generate one SVA for each of the provided natural language test plans. 
    Enclose each SVA in triple backticks (```) and prefix it with 'SVA:'. 
    Each SVA should be in the following format:
    
    SVA:
    ```
    @(posedge PCLK) <condition> |-> <consequence>;
    ```

    Use the following examples as a guide:

    SVA:
    ```
    @(posedge PCLK) ((PWDATA >= 230) && (PWDATA <= 255)) |-> (PWDATA >= 205) && (PWDATA <= 255);
    ```
    NL: that when PWDATA is within the range of 230 to 255, in the next cycle PWDATA will remain within the range of 205 to 255. Use the signals 'PCLK' for the clock edge and 'PWDATA' for the data being checked.

    SVA:
    ```
    @(posedge PCLK) (PRESETn) |-> (PWDATA >= 0) && (PWDATA <= 45);
    ```
    NL: that the input data is within the valid range when not in reset. Use the signals 'PRESETn', 'PCLK', and 'PWDATA'.

    Ensure that each SVA is a complete and valid System Verilog assertion.
    """

    if valid_signals:
        sva_gen_prompt += "IMPORTANT: Only use the signal names provided in the 'Valid Signal Names' list above. Do not introduce any new signal names."

    return sva_gen_prompt


def parse_nl_plans(result: str) -> Dict[str, List[str]]:
    nl_plans = {}
    current_signal = None

    for line in result.split('\n'):
        line = line.strip()
        if not line:
            continue

        if ':' in line:
            parts = line.split(':', 1)
            if len(parts) == 2:
                signal, plan = parts
                signal = signal.strip()
                plan = plan.strip()

                if signal not in nl_plans:
                    nl_plans[signal] = []

                nl_plans[signal].append(plan)
                current_signal = signal
        elif current_signal:
            # If there's no colon but we have a current signal, assume it's a continuation of the previous plan
            nl_plans[current_signal][-1] += " " + line

    return nl_plans


def write_svas_to_file(svas: List[str]) -> Tuple[List[str], Set[str]]:
    """
    Write each generated SVA to a separate file, preserving the module interface from the original file.

    Args:
        svas (List[str]): List of generated SVAs.

    Returns:
        Tuple[List[str], Set[str]]: List of paths to the generated SVA files and set of valid signal names.
    """
    original_sva_path = os.path.join(FLAGS.design_dir, "property_goldmine.sva")

    # Extract the module interface from the original file
    with open(original_sva_path, "r") as f:
        original_content = f.read()

    # Use regex to find the module declaration
    module_match = re.search(
        r'module\s+(\w+)\s*\((.*?)\);', original_content, re.DOTALL
    )
    if not module_match:
        raise ValueError("Could not find module declaration in the original SVA file.")

    module_name = module_match.group(1)
    module_interface = f"module {module_name}({module_match.group(2)});"

    # Extract valid signal names
    valid_signals = extract_signal_names(module_interface)

    sva_file_paths = []
    for i, sva in enumerate(svas):
        sva_file_content = f"{module_interface}\n\n"

        # Format the SVA as a property
        property_name = f"a{i}"
        sva_file_content += f"property {property_name};\n"
        sva_file_content += f"{sva}\n"
        sva_file_content += f"endproperty\n"
        sva_file_content += (
            f"assert_{property_name}: assert property({property_name});\n\n"
        )
        sva_file_content += "endmodule\n"

        sva_file_path = os.path.join(saver.logdir, "tbs", f"property_goldmine_{i}.sva")
        os.makedirs(os.path.dirname(sva_file_path), exist_ok=True)
        with open(sva_file_path, "w") as f:
            f.write(sva_file_content)
        sva_file_paths.append(sva_file_path)

    return sva_file_paths, valid_signals


def generate_tcl_scripts(sva_file_paths: List[str]) -> List[str]:
    """
    Generate TCL scripts for JasperGold, one for each SVA file.

    Args:
        sva_file_paths (List[str]): Paths to the generated SVA files.

    Returns:
        List[str]: Paths to the generated TCL scripts.
    """
    design_dir = FLAGS.design_dir
    if not os.path.exists(design_dir):
        raise Exception(f"Design directory {design_dir} does not exist")

    # Find the original TCL file
    original_tcl_path = find_original_tcl_file(design_dir)
    if not original_tcl_path:
        raise Exception("Could not find the original TCL file")

    # Read the original TCL content
    with open(original_tcl_path, 'r') as f:
        original_tcl_content = f.read()

    tcl_file_paths = []
    for i, sva_file_path in enumerate(sva_file_paths):
        # Modify the TCL content
        modified_tcl_content = modify_tcl_content(original_tcl_content, sva_file_path)

        tcl_file_path = os.path.join(
            saver.logdir, "tcl_scripts", f"FPV_{os.path.basename(design_dir)}_{i}.tcl"
        )
        os.makedirs(os.path.dirname(tcl_file_path), exist_ok=True)
        with open(tcl_file_path, "w") as f:
            f.write(modified_tcl_content)
        tcl_file_paths.append(tcl_file_path)

    return tcl_file_paths


def modify_tcl_content(original_content: str, new_sva_path: str) -> str:
    """
    Modify the TCL content to use the new SVA file.

    Args:
        original_content (str): Original TCL file content.
        new_sva_path (str): Path to the new SVA file.

    Returns:
        str: Modified TCL content.
    """
    # Replace the property_goldmine.sva file path
    modified_content = re.sub(
        r'(\$\{RTL_PATH\}/bindings\.sva\s*\\\s*)\$\{RTL_PATH\}/property_goldmine\.sva',
        f'\\1{new_sva_path}',
        original_content,
    )

    return modified_content


def run_jaspergold(tcl_file_paths: List[str]) -> List[str]:
    """
    Run JasperGold using the generated TCL scripts for each SVA.

    Args:
        tcl_file_paths (List[str]): Paths to the TCL scripts.

    Returns:
        List[str]: List of paths to JasperGold report files.
    """
    jasper_reports = []
    for i, tcl_file_path in tqdm(
        enumerate(tcl_file_paths),
        total=len(tcl_file_paths),
    ):
        # Create a unique project directory
        project_dir = os.path.join(saver.logdir, 'jgproject', f"jgproject_{i}")
        os.makedirs(project_dir, exist_ok=True)

        jasper_command = f"/<path>/<to>/jg -batch -proj {project_dir} -tcl {tcl_file_path}"

        try:
            result = subprocess.run(
                jasper_command,
                shell=True,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=FLAGS.design_dir,
            )

            report = result.stdout

            print(f"JasperGold for SVA {i} exited with code: {result.returncode}")

            # if result.returncode != 0:
            #     print(f"Warning: JasperGold for SVA {i} returned non-zero exit status.")
            # print(f"Command output:\n{report}")

        except Exception as e:
            print(f"Error running JasperGold for SVA {i}: {str(e)}")
            report = f"Error: {str(e)}\n"

        report_file_path = os.path.join(
            saver.logdir, "jasper_reports", f"jasper_report_{i}.txt"
        )
        os.makedirs(os.path.dirname(report_file_path), exist_ok=True)
        with open(report_file_path, "w") as f:
            f.write(report)
        jasper_reports.append(report_file_path)

        # Optional: Clean up the project directory
        # shutil.rmtree(project_dir)

    return jasper_reports


def run_jasper_flow(svas: List[str], sva_file_paths: List[str]) -> Dict:
    """
    Preserve the original JasperGold flow behind a backend wrapper.
    """
    print("JasperGold backend: generating TCL scripts...")
    tcl_file_paths = generate_tcl_scripts(sva_file_paths)

    print("JasperGold backend: running formal verification...")
    jasper_reports = run_jaspergold(tcl_file_paths)

    print("JasperGold backend: analyzing coverage of proven SVAs...")
    coverage_report = analyze_coverage_of_proven_svas(svas, jasper_reports)

    return {
        "tcl_file_paths": tcl_file_paths,
        "jasper_reports": jasper_reports,
        "coverage_report": coverage_report,
    }


def run_verification_backends(svas: List[str], sva_file_paths: List[str]) -> Dict:
    """
    Dispatch generated SVAs to the configured verification backend(s).
    """
    backend = getattr(FLAGS, "verification_backend", "jasper")
    valid_backends = {"jasper", "verilator", "both"}
    if backend not in valid_backends:
        raise ValueError(
            f"Unsupported verification_backend='{backend}'. "
            f"Use one of {sorted(valid_backends)}."
        )

    results = {
        "verification_backend": backend,
        "jasper": None,
        "verilator": None,
        "jasper_reports": [],
        "coverage_report": "",
    }

    if backend in ["jasper", "both"]:
        jasper_result = run_jasper_flow(svas, sva_file_paths)
        results["jasper"] = jasper_result
        results["jasper_reports"] = jasper_result["jasper_reports"]
        results["coverage_report"] = jasper_result["coverage_report"]

    if backend in ["verilator", "both"]:
        verilator_result = run_verilator_flow(
            sva_file_paths=sva_file_paths,
            design_dir=FLAGS.design_dir,
            testbench_path=getattr(FLAGS, "verilator_testbench_path", None),
            top_module=getattr(FLAGS, "verilator_top_module", None),
        )
        results["verilator"] = verilator_result

    return results


def analyze_results(
    pdf_stats: dict,
    nl_plans: Dict[str, List[str]],
    svas: List[str],
    jasper_reports: List[str],
    coverage_report: str,
    backend_results: Optional[Dict] = None,
):
    """
    Analyze and print statistics about the generated plans, SVAs, JasperGold results, and coverage report.

    Args:
        pdf_stats (dict): Statistics about the input PDF file.
        nl_plans (Dict[str, List[str]]): Dictionary of generated natural language test plans.
        svas (List[str]): List of generated SVAs.
        jasper_reports (List[str]): List of paths to JasperGold report files.
        coverage_report (str): Coverage report from JasperGold.
    """
    backend_results = backend_results or {}
    detailed_report_path = None

    # General statistics
    print("\nGeneral Statistics:")
    print("PDF Statistics:")
    print(f"  Number of pages: {pdf_stats['num_pages']}")
    print(f"  Number of tokens: {pdf_stats['num_tokens']}")
    print(f"  File size: {pdf_stats['file_size']} bytes")

    print("\nNatural Language Test Plans:")
    if nl_plans:
        total_plans = sum(len(plans) for plans in nl_plans.values())
        print(f"  Total number of plans generated: {total_plans}")
        print(f"  Number of signals with plans: {len(nl_plans)}")
        avg_plans_per_signal = total_plans / len(nl_plans) if nl_plans else 0
        print(f"  Average plans per signal: {avg_plans_per_signal:.2f}")

        all_plans = [plan for plans in nl_plans.values() for plan in plans]
        avg_plan_length = (
            sum(len(plan.split()) for plan in all_plans) / len(all_plans)
            if all_plans
            else 0
        )
        print(f"  Average plan length: {avg_plan_length:.2f} words")
    else:
        print(f'nl_plans={nl_plans}')

    print("\nSVAs:")
    print(f"  Number of SVAs generated: {len(svas)}")
    avg_sva_length = sum(len(sva.split()) for sva in svas) / len(svas) if svas else 0
    print(f"  Average SVA length: {avg_sva_length:.2f} words")

    if jasper_reports:
        detailed_report_path = generate_detailed_sva_report(svas, jasper_reports)

        print("\nJasperGold Results Summary:")
        with open(detailed_report_path, 'r') as f:
            df = pd.read_csv(f)

        proven_count = sum(df['Proof Status'] == 'proven')
        cex_count = sum(df['Proof Status'] == 'cex')
        inconclusive_count = sum(df['Proof Status'] == 'inconclusive')
        error_count = sum(df['Proof Status'] == 'error')
        syntax_correct_count = len(svas) - error_count

        print(f"  Total SVAs evaluated: {len(jasper_reports)}")
        print(f"  Proven: {proven_count}")
        print(f"  Counterexample found: {cex_count}")
        print(f"  Inconclusive: {inconclusive_count}")
        print(f"  Errors: {error_count}")
        print(f"  Syntax-correct SVAs: {syntax_correct_count}")

        success_rate = proven_count / len(jasper_reports) if jasper_reports else 0
        print(f"  Success rate: {success_rate:.2%}")
    else:
        print("\nJasperGold Results Summary:")
        print("  JasperGold backend was not run.")
        proven_count = 0
        syntax_correct_count = 0

    print("\nDetailed results saved in:")
    print(f"  SVA files: {os.path.join(saver.logdir, 'tbs')}")
    if jasper_reports:
        print(f"  Jasper reports: {os.path.join(saver.logdir, 'jasper_reports')}")
        print(f"  Detailed SVA report: {detailed_report_path}")

    verilator_result = backend_results.get("verilator")
    if verilator_result:
        print("\nVerilator Results Summary:")
        print(f"  Backend status: {verilator_result.get('status')}")
        print(f"  Compile status: {verilator_result.get('compile_status')}")
        print(f"  Simulation status: {verilator_result.get('simulation_status')}")
        print(
            f"  Assertion failures: "
            f"{len(verilator_result.get('assertion_failures', []))}"
        )
        print(f"  Report: {verilator_result.get('report_json_path')}")

    print("\nCoverage Report:")
    # coverage_lines = coverage_report.split('\n')
    # try:
    #     start_index = coverage_lines.index("COVERAGE REPORT")
    #     for line in coverage_lines[start_index:]:
    #         print(line)
    # except ValueError:
    #     print("Coverage report format is unexpected. Raw output:")
    #     print(coverage_report)

    # Extract critical coverage metrics
    coverage_metrics = calculate_coverage_metric(coverage_report)

    # Calculate final metrics
    total_assertions = len(svas)
    syntax_correct_assertions = syntax_correct_count
    proven_assertions = proven_count
    syntax_correction_rate = (
        syntax_correct_assertions / total_assertions if total_assertions else 0
    )
    proven_rate = proven_assertions / total_assertions if total_assertions else 0

    # Print final results in tab-separated format
    print("\nFinal Results (Tab-separated for easy copying to spreadsheets):")
    metric_names = [
        "# Assertions",
        "# Syntax Correct Assertions",
        "# Proven Assertions",
        "Syntax Correction Rate",
        "Pass/Proven Rate",
        "Stimuli Statement Coverage",
        "Stimuli Branch Coverage",
        "Stimuli Functional Coverage",
        "Stimuli Toggle Coverage",
        "Stimuli Expression Coverage",
        "COI Statement Coverage",
        "COI Branch Coverage",
        "COI Functional Coverage",
        "COI Toggle Coverage",
        "COI Expression Coverage",
    ]
    print("\t".join(metric_names))

    metric_values = [
        f"{total_assertions}",
        f"{syntax_correct_assertions}",
        f"{proven_assertions}",
        f"{syntax_correction_rate:.4f}",
        f"{proven_rate:.4f}",
        f"{coverage_metrics.get('coverage_stimuli_statement', 0):.4f}",
        f"{coverage_metrics.get('coverage_stimuli_branch', 0):.4f}",
        f"{coverage_metrics.get('coverage_stimuli_functional', 0):.4f}",
        f"{coverage_metrics.get('coverage_stimuli_toggle', 0):.4f}",
        f"{coverage_metrics.get('coverage_stimuli_expression', 0):.4f}",
        f"{coverage_metrics.get('coverage_coi_statement', 0):.4f}",
        f"{coverage_metrics.get('coverage_coi_branch', 0):.4f}",
        f"{coverage_metrics.get('coverage_coi_functional', 0):.4f}",
        f"{coverage_metrics.get('coverage_coi_toggle', 0):.4f}",
        f"{coverage_metrics.get('coverage_coi_expression', 0):.4f}",
    ]
    print("\t".join(metric_values))


def calculate_coverage_metric(jasper_out_str):
    coverage_dict = {
        "stimuli_statement": 0.0,
        "stimuli_branch": 0.0,
        "stimuli_functional": 0.0,
        "stimuli_toggle": 0.0,
        "stimuli_expression": 0.0,
        "coi_statement": 0.0,
        "coi_branch": 0.0,
        "coi_functional": 0.0,
        "coi_toggle": 0.0,
        "coi_expression": 0.0,
    }

    # Extract coverage metrics
    coverage_matches = re.findall(r"(\w+)\|(\w+)\|(\d+\.\d+)", jasper_out_str)
    key_map = {
        ("coi", "statement"): "coi_statement",
        ("coi", "branch"): "coi_branch",
        ("coi", "functional"): "coi_functional",
        ("coi", "toggle"): "coi_toggle",
        ("coi", "expression"): "coi_expression",
        ("stimuli", "statement"): "stimuli_statement",
        ("stimuli", "branch"): "stimuli_branch",
        ("stimuli", "functional"): "stimuli_functional",
        ("stimuli", "toggle"): "stimuli_toggle",
        ("stimuli", "expression"): "stimuli_expression",
    }

    for category, model, value in coverage_matches:
        key = key_map.get((category, model))
        if key:
            coverage_dict[key] = float(value)

    # Initialize metric
    metric = {
        "syntax": 1.0,
        "functionality": 0.0,
        **{f"coverage_{k}": v for k, v in coverage_dict.items()},
    }

    # Check for syntax errors in the output
    if re.search(r"ERROR \(VERI-", jasper_out_str, re.IGNORECASE):
        metric["syntax"] = 0.0
        metric["functionality"] = 0.0
    # Search for proof results in the output

    if re.search(r"syntax error", jasper_out_str, re.IGNORECASE):
        metric["syntax"] = 0.0
        metric["functionality"] = 0.0
    # Search for proof results in the output
    proof_result_match = re.findall(r"\bproofs:[^\n]*", jasper_out_str)
    # coverage_result_match = re.findall(r"\bcoverage:[^\n]*", jasper_out_str)
    # print(f"Proof_result_match: {proof_result_match}")

    if not proof_result_match:
        metric["functionality"] = 0.0
        return metric

    proof_result_list = proof_result_match[-1].split(":")[-1].strip().split()
    # if coverage_result_match:
    # Proceed only if there's a match in coverage_result_match
    # coverage_result_list = coverage_result_match[-1].split(":")[-1].strip().split()

    # Count number of "proven" assertions
    if proof_result_list.count("proven") != 0:
        metric["functionality"] = 1.0

    # if FLAGS.both_cover_and_assertion:
    #   if coverage_result_match:
    #     if coverage_result_list.count("covered") == 0:
    #       metric["functionality"] = 0.0
    #   else:
    #     metric["functionality"] = 0.0

    return metric


def generate_detailed_sva_report(svas: List[str], jasper_reports: List[str]) -> str:
    """
    Generate a detailed report for each SVA, including syntax correctness, FPV status, and error messages.

    Args:
        svas (List[str]): List of generated SVAs.
        jasper_reports (List[str]): List of paths to JasperGold report files.

    Returns:
        str: Path to the generated CSV file containing the detailed report.
    """
    sva_details = []
    syntax_correct_count = 0
    for i, (sva, report_path) in enumerate(zip(svas, jasper_reports)):
        with open(report_path, 'r') as f:
            report_content = f.read()

        proof_status = extract_proof_status(report_content)

        if proof_status != "error":
            syntax_correct_count += 1

        error_message = ""
        if proof_status == "error":
            error_message = extract_short_error_message(
                extract_error_message(report_content)
            )

        sva_details.append(
            {"SVA ID": i, "Proof Status": proof_status, "Error Message": error_message}
        )

    # Create a DataFrame and save it as a CSV
    df = pd.DataFrame(sva_details)
    csv_path = os.path.join(saver.logdir, "sva_details.csv")
    df.to_csv(csv_path, index=False)

    # Print the table
    print("\nDetailed SVA Results:")
    print(tabulate(df, headers='keys', tablefmt='grid', showindex=False))

    print(f"\nSyntax-correct SVAs: {syntax_correct_count} out of {len(svas)}")
    print(f"\nDetailed SVA results saved to: {csv_path}")

    return csv_path


def extract_error_message(report_content: str) -> str:
    """
    Extract the first error message from the JasperGold report.

    Args:
        report_content (str): Content of the JasperGold report.

    Returns:
        str: The first error message found, or "Unknown error" if none found.
    """
    error_lines = [line for line in report_content.split('\n') if "ERROR" in line]
    return error_lines[0] if error_lines else "Unknown error"


def log_llm_interaction(prompt: str, response: str, interaction_type: str):
    """
    Log the prompt and response from LLM interactions to a file.

    Args:
        prompt (str): The prompt sent to the LLM.
        response (str): The response received from the LLM.
        interaction_type (str): Type of interaction (e.g., 'NL_Plans', 'SVAs').
    """
    log_file_path = os.path.join(saver.logdir, 'llm_interactions.txt')
    with open(log_file_path, 'a') as f:
        f.write(f"\n\n{'=' * 50}\n")
        f.write(f"Interaction Type: {interaction_type}\n")
        f.write(f"{'=' * 50}\n")
        f.write("Prompt:\n")
        f.write(prompt)
        f.write("\n\nResponse:\n")
        f.write(response)
        f.write("\n\n")


def extract_short_error_message(full_error: str) -> str:
    """
    Extract a short version of the error message.

    Args:
        full_error (str): The full error message.

    Returns:
        str: A shortened version of the error message.
    """
    # Look for the main error description, typically after the last colon
    parts = full_error.split(':')
    if len(parts) > 1:
        return parts[-1].strip()
    return full_error


def extract_signal_names(module_interface: str) -> Set[str]:
    """
    Extract signal names from the module interface.

    Args:
        module_interface (str): The module interface declaration.

    Returns:
        Set[str]: A set of signal names found in the interface.
    """
    # Regular expression to match signal declarations
    signal_pattern = (
        r'\b(?:input|output|inout)\s+(?:reg|wire)?\s*(?:\[[^\]]+\])?\s*(\w+)'
    )

    # Find all matches
    matches = re.findall(signal_pattern, module_interface)

    # Extract signal names from matches
    signal_names = set(matches)

    return signal_names

