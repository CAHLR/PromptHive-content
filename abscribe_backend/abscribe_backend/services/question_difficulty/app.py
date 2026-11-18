#!/usr/bin/env python3
"""Streamlit UI for LLM-IRT Replication Tool"""
import streamlit as st
import pandas as pd
import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime
import os
from dotenv import load_dotenv
import asyncio

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from utils.io import load_items, save_csv
from scripts.generate_responses import generate_responses_async
import argparse

# Load environment variables
load_dotenv()

st.set_page_config(
    page_title="LLM-IRT Analysis Tool",
    layout="wide"
)

st.title("LLM-IRT Analysis Tool")
st.markdown("*Generate synthetic respondent data using LLMs and analyze with Rasch IRT models*")

# Sidebar for configuration
st.sidebar.header("Configuration")

# Check if OpenAI API key is set
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    st.sidebar.error("OPENAI_API_KEY not found in .env file")
    st.sidebar.info("Please create a .env file with your OpenAI API key")
else:
    st.sidebar.success("API Key configured")

# Model selection
model_options = ["gpt-3.5-turbo", "gpt-4", "gpt-4-turbo"]
default_model = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
model_name = st.sidebar.selectbox(
    "Model",
    model_options,
    index=model_options.index(default_model) if default_model in model_options else 0
)

# Tabs for different steps
tab1, tab2, tab3, tab4 = st.tabs([
    "Step 1: Items Data",
    "Step 2: Generate Responses",
    "Step 3: Run IRT Analysis",
    "Step 4: View Results"
])

# ===== TAB 1: Items Data =====
with tab1:
    st.header("Manage Items Dataset")

    items_file = Path("data/items.csv")

    col1, col2 = st.columns([2, 1])

    with col1:
        st.subheader("Upload or Edit Items")

        # File uploader
        uploaded_file = st.file_uploader(
            "Upload items CSV file",
            type=['csv'],
            help="CSV with columns: item_id, stem, answer_key, answer_type, mc_choices"
        )

        if uploaded_file:
            try:
                df = pd.read_csv(uploaded_file)
                # Save uploaded file
                items_file.parent.mkdir(parents=True, exist_ok=True)
                df.to_csv(items_file, index=False)
                st.success(f"Uploaded {len(df)} items")
            except Exception as e:
                st.error(f"Error reading file: {e}")

    with col2:
        st.subheader("Current Items")
        if items_file.exists():
            try:
                current_df = pd.read_csv(items_file)
                st.metric("Total Items", len(current_df))

                # Show answer type distribution
                if 'answer_type' in current_df.columns:
                    type_counts = current_df['answer_type'].value_counts()
                    st.write("**Answer Types:**")
                    for atype, count in type_counts.items():
                        st.write(f"- {atype}: {count}")
            except Exception as e:
                st.error(f"Error loading items: {e}")
        else:
            st.info("No items file found")

    # Display current items
    if items_file.exists():
        st.subheader("Current Items Preview")
        try:
            df = pd.read_csv(items_file)
            st.dataframe(df, use_container_width=True, height=300)

            # Download button
            csv = df.to_csv(index=False)
            st.download_button(
                "Download Current Items",
                csv,
                "items.csv",
                "text/csv",
                key='download-items'
            )
        except Exception as e:
            st.error(f"Error displaying items: {e}")

    # Template download
    st.subheader("Template")
    template_data = {
        'item_id': ['Q1', 'Q2'],
        'stem': [
            'Given m = 4, find the equation of the line in slope-intercept form passing through the point (2, 5).',
            'State whether the two lines are parallel, perpendicular or neither: 3y = -4x + 3 and 3x - 4y = 8.'
        ],
        'answer_key': ['y=4x-3', 'neither'],
        'answer_type': ['algebra', 'mc'],
        'mc_choices': ['', 'parallel|perpendicular|neither']
    }
    template_df = pd.DataFrame(template_data)
    template_csv = template_df.to_csv(index=False)

    st.download_button(
        "Download Template CSV",
        template_csv,
        "items_template.csv",
        "text/csv",
        help="Download a template CSV file with example items"
    )

# ===== TAB 2: Generate Responses =====
with tab2:
    st.header("Generate LLM Responses")

    items_file = Path("data/items.csv")

    if not items_file.exists():
        st.warning("Please upload items data in Step 1 first")
    else:
        # Load items to show count
        try:
            items_df = pd.read_csv(items_file)
            st.info(f"Loaded **{len(items_df)} items** from {items_file}")
        except Exception as e:
            st.error(f"Error loading items: {e}")
            items_df = None

        if items_df is not None:
            col1, col2 = st.columns(2)

            with col1:
                n_respondents = st.number_input(
                    "Number of Respondents",
                    min_value=10,
                    max_value=1000,
                    value=150,
                    step=10,
                    help="Number of synthetic respondents to generate"
                )

                temperature = st.slider(
                    "Temperature",
                    min_value=0.0,
                    max_value=2.0,
                    value=0.5,
                    step=0.1,
                    help="Higher values = more randomness"
                )

            with col2:
                seed = st.number_input(
                    "Random Seed",
                    min_value=0,
                    max_value=9999,
                    value=42,
                    help="For reproducibility"
                )

                use_async = st.checkbox(
                    "Use Async (Faster)",
                    value=True,
                    help="Run API calls concurrently - faster but may hit rate limits"
                )

            # Run name
            default_run_name = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            run_name = st.text_input(
                "Run Name",
                value=default_run_name,
                help="Name for this run (will be saved in runs/ directory)"
            )

            # Estimate
            total_calls = n_respondents * len(items_df)
            st.info(f"This will make **{total_calls:,}** API calls ({n_respondents} respondents × {len(items_df)} items)")

            if st.button("Generate Responses", type="primary", use_container_width=True):
                if not api_key:
                    st.error("Please configure OPENAI_API_KEY in .env file")
                else:
                    out_dir = Path("runs") / run_name

                    with st.spinner(f"Generating responses... (this may take several minutes)"):
                        try:
                            # Create args object
                            args = argparse.Namespace(
                                items=str(items_file),
                                n_respondents=n_respondents,
                                temperature=temperature,
                                seed=seed,
                                model_name=model_name,
                                out_dir=str(out_dir),
                                use_async=use_async
                            )

                            # Progress bar
                            progress_bar = st.progress(0)
                            status_text = st.empty()

                            if use_async:
                                status_text.text("Running async generation...")
                                asyncio.run(generate_responses_async(args))
                            else:
                                # Run synchronously
                                from scripts.generate_responses import generate_responses
                                status_text.text("Running generation...")
                                generate_responses(args)

                            progress_bar.progress(100)
                            status_text.empty()

                            st.success(f"Responses generated successfully!")
                            st.balloons()

                            # Show results summary
                            run_json = out_dir / 'run.json'
                            if run_json.exists():
                                with open(run_json, 'r') as f:
                                    metadata = json.load(f)

                                st.subheader("Results Summary")
                                col1, col2, col3 = st.columns(3)
                                col1.metric("Overall Accuracy", f"{metadata['overall_accuracy']:.2%}")
                                col2.metric("Respondents", metadata['n_respondents'])
                                col3.metric("Items", metadata['n_items'])

                                # Save run name for next step
                                st.session_state['last_run'] = run_name

                        except Exception as e:
                            st.error(f"Error generating responses: {e}")
                            import traceback
                            st.code(traceback.format_exc())

# ===== TAB 3: Run IRT Analysis =====
with tab3:
    st.header("Run Rasch IRT Analysis")

    # List available runs
    runs_dir = Path("runs")
    if runs_dir.exists():
        run_dirs = [d.name for d in runs_dir.iterdir() if d.is_dir()]
        run_dirs.sort(reverse=True)  # Most recent first
    else:
        run_dirs = []

    if not run_dirs:
        st.warning("No runs found. Please generate responses in Step 2 first.")
    else:
        # Pre-select last run if available
        default_idx = 0
        if 'last_run' in st.session_state and st.session_state['last_run'] in run_dirs:
            default_idx = run_dirs.index(st.session_state['last_run'])

        selected_run = st.selectbox(
            "Select Run",
            run_dirs,
            index=default_idx,
            help="Choose which run to analyze"
        )

        run_path = runs_dir / selected_run
        responses_file = run_path / "responses.csv"

        if not responses_file.exists():
            st.error(f"responses.csv not found in {run_path}")
        else:
            # Show run info
            run_json = run_path / "run.json"
            if run_json.exists():
                with open(run_json, 'r') as f:
                    metadata = json.load(f)

                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Respondents", metadata.get('n_respondents', 'N/A'))
                col2.metric("Items", metadata.get('n_items', 'N/A'))
                col3.metric("Accuracy", f"{metadata.get('overall_accuracy', 0):.2%}")
                col4.metric("Model", metadata.get('model', 'N/A'))

            st.divider()

            # Check if R is available
            try:
                result = subprocess.run(['Rscript', '--version'],
                                      capture_output=True, text=True, timeout=5)
                r_available = result.returncode == 0
            except:
                r_available = False

            if not r_available:
                st.error("R is not installed or not in PATH")
                st.info("Please install R and ensure Rscript is available in your PATH")
            else:
                st.success("R is available")

                if st.button("Run Rasch IRT Analysis", type="primary", use_container_width=True):
                    item_params_file = run_path / "item_params.csv"
                    fit_summary_file = run_path / "fit_summary.json"

                    with st.spinner("Running Rasch IRT model... (this may take a minute)"):
                        try:
                            # Run R script
                            r_script = Path("scripts/run_rasch.R")

                            cmd = [
                                'Rscript',
                                str(r_script),
                                str(responses_file),
                                str(item_params_file),
                                str(fit_summary_file)
                            ]

                            result = subprocess.run(
                                cmd,
                                capture_output=True,
                                text=True,
                                timeout=300  # 5 minute timeout
                            )

                            if result.returncode == 0:
                                st.success("IRT analysis completed successfully!")
                                st.session_state['last_analysis_run'] = selected_run

                                # Show results
                                if item_params_file.exists():
                                    st.subheader("Item Parameters")
                                    params_df = pd.read_csv(item_params_file)
                                    st.dataframe(params_df, use_container_width=True)

                                if fit_summary_file.exists():
                                    with open(fit_summary_file, 'r') as f:
                                        fit_data = json.load(f)

                                    st.subheader("Model Fit Statistics")
                                    col1, col2, col3 = st.columns(3)
                                    col1.metric("Log-Likelihood", f"{fit_data.get('logLik', 'N/A'):.2f}")
                                    col2.metric("AIC", f"{fit_data.get('AIC', 'N/A'):.2f}")
                                    col3.metric("BIC", f"{fit_data.get('BIC', 'N/A'):.2f}")

                            else:
                                st.error("IRT analysis failed")
                                st.code(result.stderr)

                        except subprocess.TimeoutExpired:
                            st.error("Analysis timed out (>5 minutes)")
                        except Exception as e:
                            st.error(f"Error running analysis: {e}")

# ===== TAB 4: View Results =====
with tab4:
    st.header("View Results")

    # List available runs
    runs_dir = Path("runs")
    if runs_dir.exists():
        run_dirs = [d.name for d in runs_dir.iterdir() if d.is_dir()]
        run_dirs.sort(reverse=True)
    else:
        run_dirs = []

    if not run_dirs:
        st.warning("No runs found.")
    else:
        # Pre-select last analyzed run if available
        default_idx = 0
        if 'last_analysis_run' in st.session_state and st.session_state['last_analysis_run'] in run_dirs:
            default_idx = run_dirs.index(st.session_state['last_analysis_run'])

        selected_run = st.selectbox(
            "Select Run to View",
            run_dirs,
            index=default_idx,
            key='view_run_select'
        )

        run_path = runs_dir / selected_run

        # Tabs for different result views
        result_tab1, result_tab2, result_tab3 = st.tabs([
            "Item Parameters",
            "Response Data",
            "Visualizations"
        ])

        with result_tab1:
            item_params_file = run_path / "item_params.csv"
            fit_summary_file = run_path / "fit_summary.json"

            if item_params_file.exists():
                st.subheader("Item Difficulty Parameters")
                params_df = pd.read_csv(item_params_file)

                # Sort by difficulty
                params_df_sorted = params_df.sort_values('difficulty_logit', ascending=False)

                st.dataframe(params_df_sorted, use_container_width=True)

                # Download button
                csv = params_df_sorted.to_csv(index=False)
                st.download_button(
                    "📥 Download Item Parameters",
                    csv,
                    f"{selected_run}_item_params.csv",
                    "text/csv"
                )

                # Show fit statistics
                if fit_summary_file.exists():
                    st.subheader("Model Fit Statistics")
                    with open(fit_summary_file, 'r') as f:
                        fit_data = json.load(f)

                    col1, col2, col3, col4 = st.columns(4)
                    col1.metric("Log-Likelihood", f"{fit_data.get('logLik', 'N/A'):.2f}")
                    col2.metric("AIC", f"{fit_data.get('AIC', 'N/A'):.2f}")
                    col3.metric("BIC", f"{fit_data.get('BIC', 'N/A'):.2f}")
                    col4.metric("Converged", "Yes" if fit_data.get('converged', False) else "No")
            else:
                st.info("⚠️ Item parameters not found. Run IRT analysis in Step 3.")

        with result_tab2:
            # Show response data
            responses_long = run_path / "responses_long.csv"
            responses_wide = run_path / "responses.csv"
            run_json = run_path / "run.json"

            if run_json.exists():
                st.subheader("Run Metadata")
                with open(run_json, 'r') as f:
                    metadata = json.load(f)

                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Respondents", metadata.get('n_respondents', 'N/A'))
                col2.metric("Items", metadata.get('n_items', 'N/A'))
                col3.metric("Overall Accuracy", f"{metadata.get('overall_accuracy', 0):.2%}")
                col4.metric("Temperature", metadata.get('temperature', 'N/A'))

                st.json(metadata, expanded=False)

            if responses_long.exists():
                st.subheader("Response Data (Long Format)")
                long_df = pd.read_csv(responses_long)

                # Filter options
                col1, col2 = st.columns(2)
                with col1:
                    filter_respondent = st.multiselect(
                        "Filter by Respondent",
                        options=long_df['respondent_id'].unique(),
                        default=None
                    )
                with col2:
                    filter_item = st.multiselect(
                        "Filter by Item",
                        options=long_df['item_id'].unique(),
                        default=None
                    )

                # Apply filters
                filtered_df = long_df.copy()
                if filter_respondent:
                    filtered_df = filtered_df[filtered_df['respondent_id'].isin(filter_respondent)]
                if filter_item:
                    filtered_df = filtered_df[filtered_df['item_id'].isin(filter_item)]

                st.dataframe(filtered_df, use_container_width=True, height=400)

                # Download
                csv = filtered_df.to_csv(index=False)
                st.download_button(
                    "📥 Download Response Data",
                    csv,
                    f"{selected_run}_responses_long.csv",
                    "text/csv"
                )

            if responses_wide.exists():
                st.subheader("Response Matrix (Wide Format)")
                wide_df = pd.read_csv(responses_wide, index_col=0)
                st.dataframe(wide_df, use_container_width=True, height=300)

        with result_tab3:
            st.subheader("Visualizations")

            item_params_file = run_path / "item_params.csv"

            if item_params_file.exists():
                params_df = pd.read_csv(item_params_file)

                # Difficulty histogram
                st.subheader("Item Difficulty Distribution")
                import plotly.express as px

                fig = px.bar(
                    params_df.sort_values('difficulty_logit', ascending=False),
                    x='item_id',
                    y='difficulty_logit',
                    error_y='se_difficulty',
                    title='Item Difficulty Parameters (Logit Scale)',
                    labels={'difficulty_logit': 'Difficulty (Logit)', 'item_id': 'Item ID'}
                )
                fig.add_hline(y=0, line_dash="dash", line_color="red",
                            annotation_text="Mean Ability (θ = 0)")
                st.plotly_chart(fig, use_container_width=True)

                # Distribution
                st.subheader("Difficulty Distribution")
                fig2 = px.histogram(
                    params_df,
                    x='difficulty_logit',
                    nbins=20,
                    title='Distribution of Item Difficulties',
                    labels={'difficulty_logit': 'Difficulty (Logit)'}
                )
                st.plotly_chart(fig2, use_container_width=True)

                # Item accuracy from run metadata
                run_json = run_path / "run.json"
                if run_json.exists():
                    with open(run_json, 'r') as f:
                        metadata = json.load(f)

                    if 'item_accuracies' in metadata:
                        st.subheader("Item Accuracy vs Difficulty")

                        # Merge accuracy with difficulty
                        accuracy_df = pd.DataFrame([
                            {'item_id': k, 'accuracy': v}
                            for k, v in metadata['item_accuracies'].items()
                        ])

                        merged = params_df.merge(accuracy_df, on='item_id')

                        fig3 = px.scatter(
                            merged,
                            x='difficulty_logit',
                            y='accuracy',
                            text='item_id',
                            title='Item Accuracy vs Difficulty',
                            labels={
                                'difficulty_logit': 'Difficulty (Logit)',
                                'accuracy': 'Accuracy Rate'
                            }
                        )
                        fig3.update_traces(textposition='top center')
                        st.plotly_chart(fig3, use_container_width=True)
            else:
                st.info("⚠️ Run IRT analysis in Step 3 to see visualizations")

# Footer
st.divider()
st.markdown("""
**How to use:**
1. **Step 1:** Upload your items CSV file with questions and answer keys
2. **Step 2:** Configure parameters and generate LLM responses
3. **Step 3:** Run Rasch IRT analysis to estimate item difficulties
4. **Step 4:** View and download results
""")
