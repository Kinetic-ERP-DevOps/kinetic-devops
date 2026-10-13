"""
Sales order reporting app builder.

Demonstrates how to create a custom app that builds on kinetic-devops
services to create a higher-level application for specific business needs.

This app:
1. Pulls sales order data
2. Applies business logic filters
3. Generates summary reports
4. Exports in multiple formats

Shows how to compose multiple kinetic-devops services into a cohesive
application tier.
"""

__metadata__ = {
    'name': 'Sales Order Report Builder',
    'version': '1.0.0',
    'author': 'Analytics Team',
    'tags': ['app', 'reporting', 'sales-orders', 'analytics'],
    'dependencies': ['kinetic_devops.baq', 'kinetic_devops.report_service'],
    'usage_example': '''
from apps.sales_reporting import generate_sales_report

report = generate_sales_report(
    period='2024-Q1',
    region='North America',
    format='excel'
)
    ''',
}

from typing import Dict, List, Any, Optional, Literal
from datetime import datetime, timedelta


def generate_sales_report(
    period: str = '2024-Q1',
    region: Optional[str] = None,
    format: Literal['json', 'excel', 'csv'] = 'json'
) -> Dict[str, Any]:
    """
    Generate comprehensive sales report for a period.
    
    Args:
        period: Period to report on (e.g., '2024-Q1', '2024-01')
        region: Filter by region (None = all regions)
        format: Output format (json, excel, csv)
    
    Returns:
        Report data with summary, details, and export info
    """
    # Simulate pulling data from BAQ
    sample_data = {
        'period': period,
        'region': region or 'ALL',
        'report_date': datetime.now().isoformat(),
        'summary': {
            'total_orders': 1250,
            'total_revenue': 2500000.00,
            'average_order_value': 2000.00,
            'growth_vs_prior_period': '12.5%',
        },
        'by_region': {
            'North America': {'orders': 600, 'revenue': 1500000},
            'Europe': {'orders': 400, 'revenue': 800000},
            'APAC': {'orders': 250, 'revenue': 200000},
        },
        'top_customers': [
            {'customer': 'ABC Corp', 'orders': 45, 'revenue': 450000},
            {'customer': 'XYZ Inc', 'orders': 38, 'revenue': 380000},
            {'customer': 'Global Ltd', 'orders': 32, 'revenue': 320000},
        ],
    }
    
    return {
        'success': True,
        'report_type': 'sales_summary',
        'format': format,
        'data': sample_data,
        'export_path': f'sales_report_{period}.{format}',
        'message': f'Generated {format} report for {period}',
    }


def get_sales_trends(months: int = 12) -> Dict[str, Any]:
    """
    Get sales trends over specified months.
    
    Args:
        months: Number of months to analyze (default 12)
    
    Returns:
        Trend analysis with growth rates and patterns
    """
    return {
        'success': True,
        'period_months': months,
        'trend': 'upward',
        'growth_rate': '15.2%',
        'seasonal_patterns': {
            'Q1': 'Strong recovery after holidays',
            'Q2': 'Steady growth',
            'Q3': 'Summer slowdown',
            'Q4': 'Year-end acceleration',
        },
        'forecast': 'Positive momentum expected to continue',
    }


def calculate_sales_metrics(
    start_date: str,
    end_date: str,
    metrics: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Calculate specific sales metrics for date range.
    
    Args:
        start_date: YYYY-MM-DD format
        end_date: YYYY-MM-DD format
        metrics: Specific metrics to calculate (None = all)
                Options: ['revenue', 'orders', 'customers', 'margins']
    
    Returns:
        Calculated metrics
    """
    all_metrics = ['revenue', 'orders', 'customers', 'margins']
    to_calc = metrics or all_metrics
    
    results = {}
    if 'revenue' in to_calc:
        results['revenue'] = 2500000.00
    if 'orders' in to_calc:
        results['orders'] = 1250
    if 'customers' in to_calc:
        results['customers'] = 285
    if 'margins' in to_calc:
        results['margins'] = '32.5%'
    
    return {
        'success': True,
        'date_range': f'{start_date} to {end_date}',
        'metrics': results,
    }
