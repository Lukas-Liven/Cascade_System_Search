import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
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

    # 2. Histogram with log scale
    ax2 = axes[0, 1]
    ax2.hist(storage_data, bins=50, color='coral', edgecolor='black', alpha=0.7)
    ax2.set_xlabel('Storage Capacity (Acre-Ft)', fontsize=12, fontweight='bold')
    ax2.set_ylabel('Number of Dams', fontsize=12, fontweight='bold')
    ax2.set_title('Storage Capacity Distribution (Log Scale)', fontsize=14, fontweight='bold')
    ax2.set_xscale('log')
    ax2.grid(True, alpha=0.3, linestyle='--')

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