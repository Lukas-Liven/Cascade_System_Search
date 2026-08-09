import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
from scipy.stats import gaussian_kde

POWER_CONVERSION_FACTOR = 11800  # Ft-CFS per MW

def analyze_dam_storage(df_dams):
    """
    Analyze and visualize dam storage capacity data.

    Parameters:
    df_dams (pd.DataFrame): DataFrame containing dam information, including 'Max Storage (Acre-Ft)'.

    Returns:
    None
    """
    # Extract storage data
    storage_data = df_dams['Max Storage (Acre-Ft)'].copy()

    # Remove NaN values and convert to numeric
    storage_data = pd.to_numeric(storage_data, errors='coerce').dropna()

    # Remove zeros and negative values for meaningful analysis
    storage_data = storage_data[storage_data > 0]

    print(f"\nStorage Data Statistics:")
    print(f"Total dams with storage data: {len(storage_data)}")
    print(f"Min storage: {storage_data.min():,.0f} Acre-Ft")
    print(f"Max storage: {storage_data.max():,.0f} Acre-Ft")
    print(f"Mean storage: {storage_data.mean():,.0f} Acre-Ft")
    print(f"Median storage: {storage_data.median():,.0f} Acre-Ft")

    # Create storage bins for meaningful visualization
    bins = [0, 100, 1000, 10000, 100000, 1000000, storage_data.max()]
    labels = ['0-100', '100-1K', '1K-10K', '10K-100K', '100K-1M', '1M+']

    # Categorize dams by storage capacity
    storage_categories = pd.cut(storage_data, bins=bins, labels=labels)
    category_counts = storage_categories.value_counts().sort_index()

    # Create figure with multiple subplots
    fig, axes = plt.subplots(2, 2, figsize=(16, 12))
    fig.suptitle('Dam Storage Capacity Analysis', fontsize=16, fontweight='bold')

    # 1. Bar chart of storage categories
    ax1 = axes[0, 0]
    bars = ax1.bar(range(len(category_counts)), category_counts.values, 
                color='steelblue', edgecolor='black', alpha=0.7)
    ax1.set_xlabel('Storage Capacity (Acre-Ft)', fontsize=12, fontweight='bold')
    ax1.set_ylabel('Number of Dams', fontsize=12, fontweight='bold')
    ax1.set_title('Distribution of Dams by Storage Capacity', fontsize=14, fontweight='bold')
    ax1.set_xticks(range(len(category_counts)))
    ax1.set_xticklabels(category_counts.index, rotation=45, ha='right')
    ax1.grid(axis='y', alpha=0.3, linestyle='--')

    # Add value labels on bars
    for i, (bar, count) in enumerate(zip(bars, category_counts.values)):
        height = bar.get_height()
        ax1.text(bar.get_x() + bar.get_width()/2., height,
                f'{int(count):,}',
                ha='center', va='bottom', fontweight='bold')

    # 2. Histogram with log scale, overlaid with a Kernel Density Estimate (KDE)
    ax2 = axes[0, 1]

    # Use log-spaced bins so each bar's width is meaningful on the log-scale
    # x-axis -- linearly-spaced bins collapse almost all dams into the first
    # bin or two, since the data spans several orders of magnitude.
    log_bins = np.logspace(np.log10(storage_data.min()), np.log10(storage_data.max()), 50)

    # Normalize the histogram to density (density=True) rather than raw counts,
    # so it shares the same y-axis scale as the KDE curve -- this avoids
    # needing a secondary axis and makes the shape comparison more direct.
    ax2.hist(storage_data, bins=log_bins, color='coral', edgecolor='black',
              alpha=0.6, density=True, label='Histogram (density)')
    ax2.set_xlabel('Storage Capacity (Acre-Ft)', fontsize=12, fontweight='bold')
    ax2.set_ylabel('Density', fontsize=12, fontweight='bold')
    ax2.set_title('Storage Capacity Distribution (Log Scale)', fontsize=14, fontweight='bold')
    ax2.set_xscale('log')
    ax2.grid(True, alpha=0.3, linestyle='--')

    # Compute the KDE on log10-transformed data, since the x-axis is displayed
    # on a log scale -- fitting the KDE directly to raw (linear-scale) values
    # would produce a curve that looks distorted/compressed once plotted
    # against a log axis.
    log_storage = np.log10(storage_data)
    kde = gaussian_kde(log_storage)

    # Evaluate the KDE over an evenly-spaced grid in log-space, then convert
    # back to linear values for plotting on the log-scale x-axis.
    x_log_grid = np.linspace(log_storage.min(), log_storage.max(), 500)
    kde_density_log = kde(x_log_grid)
    x_grid = 10 ** x_log_grid

    # Important: the KDE above evaluates density with respect to log10(x), not
    # x itself. To correctly overlay it on a histogram whose bins/density are
    # measured with respect to x (linear Acre-Ft, just displayed on a log
    # axis), we must apply the change-of-variables correction:
    #   f_X(x) = f_{log10(X)}(log10(x)) / (x * ln(10))
    # Without this correction, the KDE curve's shape and scale would not
    # actually match the density=True histogram it's overlaid on.
    kde_density_linear = kde_density_log / (x_grid * np.log(10))

    ax2.plot(x_grid, kde_density_linear, color='darkred', linewidth=2, label='KDE')
    ax2.legend()

    # 3. Cumulative distribution
    ax3 = axes[1, 0]
    sorted_storage = np.sort(storage_data)
    cumulative = np.arange(1, len(sorted_storage) + 1) / len(sorted_storage) * 100
    ax3.plot(sorted_storage, cumulative, color='green', linewidth=2)
    ax3.set_xlabel('Storage Capacity (Acre-Ft)', fontsize=12, fontweight='bold')
    ax3.set_ylabel('Cumulative Percentage (%)', fontsize=12, fontweight='bold')
    ax3.set_title('Cumulative Distribution of Storage Capacity', fontsize=14, fontweight='bold')
    ax3.set_xscale('log')
    ax3.grid(True, alpha=0.3, linestyle='--')
    ax3.axhline(y=50, color='red', linestyle='--', alpha=0.5, label='50th percentile')
    ax3.axhline(y=90, color='orange', linestyle='--', alpha=0.5, label='90th percentile')
    ax3.legend()

    # 4. Top 20 dams by storage
    ax4 = axes[1, 1]
    top_20_storage = storage_data.nlargest(20)
    top_20_indices = top_20_storage.index
    top_20_names = df_dams.loc[top_20_indices, 'Dam Name'].values
    bars = ax4.barh(range(len(top_20_storage)), top_20_storage.values, 
                    color='purple', edgecolor='black', alpha=0.7)
    ax4.set_yticks(range(len(top_20_storage)))
    ax4.set_yticklabels([name[:30] + '...' if len(name) > 30 else name 
                        for name in top_20_names], fontsize=9)
    ax4.set_xlabel('Storage Capacity (Acre-Ft)', fontsize=12, fontweight='bold')
    ax4.set_title('Top 20 Dams by Storage Capacity', fontsize=14, fontweight='bold')
    ax4.grid(axis='x', alpha=0.3, linestyle='--')
    ax4.invert_yaxis()

    # Add value labels
    for i, (bar, value) in enumerate(zip(bars, top_20_storage.values)):
        width = bar.get_width()
        ax4.text(width, bar.get_y() + bar.get_height()/2.,
                f'{value:,.0f}',
                ha='left', va='center', fontsize=8, fontweight='bold')

    plt.tight_layout()
    plt.savefig('dam_storage_analysis.png', dpi=300, bbox_inches='tight')
    plt.show()

    print("\nVisualization saved as 'dam_storage_analysis.png'")

def analyze_power_capacity(df_dams):
    """
    Analyze and visualize hydroelectric dam power capacity data, and its
    relationship to storage capacity.

    Power Capacity (MW) is estimated as:
        (Hydraulic Height (Ft) * Max Discharge (Cubic Ft/Second)) / POWER_CONVERSION_FACTOR

    Parameters:
    df_dams (pd.DataFrame): DataFrame containing dam information, including
        'Hydraulic Height (Ft)', 'Max Discharge (Cubic Ft/Second)', and
        'Max Storage (Acre-Ft)'.

    Returns:
    None
    """
    # Work on a copy so we don't mutate the caller's dataframe
    df = df_dams.copy()

    # Ensure the relevant columns are numeric; invalid/non-numeric entries
    # become NaN and are dropped below, same approach as analyze_dam_storage.
    df['Hydraulic Height (Ft)'] = pd.to_numeric(df['Hydraulic Height (Ft)'], errors='coerce')
    df['Max Discharge (Cubic Ft/Second)'] = pd.to_numeric(df['Max Discharge (Cubic Ft/Second)'], errors='coerce')
    df['Max Storage (Acre-Ft)'] = pd.to_numeric(df['Max Storage (Acre-Ft)'], errors='coerce')

    # Drop rows missing any of the three fields needed for this analysis
    df = df.dropna(subset=['Hydraulic Height (Ft)', 'Max Discharge (Cubic Ft/Second)', 'Max Storage (Acre-Ft)'])

    # Compute estimated power capacity in MW
    df['Power Capacity (MW)'] = (
        df['Hydraulic Height (Ft)'] * df['Max Discharge (Cubic Ft/Second)'] / POWER_CONVERSION_FACTOR
    )

    # Keep only physically meaningful (positive) values for storage and power
    df = df[(df['Power Capacity (MW)'] > 0) & (df['Max Storage (Acre-Ft)'] > 0)]

    power_data = df['Power Capacity (MW)']

    print(f"\nPower Capacity Statistics:")
    print(f"Total hydroelectric dams with power capacity data: {len(power_data)}")
    print(f"Min power capacity: {power_data.min():,.2f} MW")
    print(f"Max power capacity: {power_data.max():,.2f} MW")
    print(f"Mean power capacity: {power_data.mean():,.2f} MW")
    print(f"Median power capacity: {power_data.median():,.2f} MW")

    # Pearson correlation between log-storage and log-power, since both
    # quantities span several orders of magnitude
    log_storage = np.log10(df['Max Storage (Acre-Ft)'])
    log_power = np.log10(power_data)
    correlation = np.corrcoef(log_storage, log_power)[0, 1]
    print(f"Correlation (log-storage vs log-power): {correlation:.3f}")

    # Create figure with two subplots side by side
    fig, axes = plt.subplots(1, 2, figsize=(16, 7))
    fig.suptitle('Hydroelectric Dam Power Capacity Analysis', fontsize=16, fontweight='bold')

    # 1. Scatter plot: Storage Capacity vs Power Capacity (log-log scale,
    # since both quantities vary over several orders of magnitude)
    ax1 = axes[0]
    ax1.scatter(df['Max Storage (Acre-Ft)'], power_data,
                color='teal', edgecolor='black', alpha=0.5, s=25)
    ax1.set_xlabel('Storage Capacity (Acre-Ft)', fontsize=12, fontweight='bold')
    ax1.set_ylabel('Power Capacity (MW)', fontsize=12, fontweight='bold')
    ax1.set_title('Storage Capacity vs. Power Capacity', fontsize=14, fontweight='bold')
    ax1.set_xscale('log')
    ax1.set_yscale('log')
    ax1.grid(True, which='both', alpha=0.3, linestyle='--')

    # 2. Cumulative distribution of power capacity
    ax2 = axes[1]
    sorted_power = np.sort(power_data)
    cumulative = np.arange(1, len(sorted_power) + 1) / len(sorted_power) * 100
    ax2.plot(sorted_power, cumulative, color='darkorange', linewidth=2)
    ax2.set_xlabel('Power Capacity (MW)', fontsize=12, fontweight='bold')
    ax2.set_ylabel('Cumulative Percentage (%)', fontsize=12, fontweight='bold')
    ax2.set_title('Cumulative Distribution of Power Capacity', fontsize=14, fontweight='bold')
    ax2.set_xscale('log')
    ax2.grid(True, alpha=0.3, linestyle='--')
    ax2.axhline(y=50, color='red', linestyle='--', alpha=0.5, label='50th percentile')
    ax2.axhline(y=90, color='blue', linestyle='--', alpha=0.5, label='90th percentile')
    ax2.legend()

    plt.tight_layout()
    plt.savefig('dam_power_capacity_analysis.png', dpi=300, bbox_inches='tight')
    plt.show()

    print("\nVisualization saved as 'dam_power_capacity_analysis.png'")