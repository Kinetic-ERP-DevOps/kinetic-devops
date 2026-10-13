"""
Purchase Order Approval BPM.

Implements Business Process Module (BPM) logic for automated
PO approvals based on configurable rules and thresholds.

This demonstrates:
1. Workflow state management
2. Rule-based decision logic
3. Multi-level approvals
4. Audit trail and notifications

Typical BPM flow:
PO Created → Pre-screening → Route to Approver → Approve/Reject → Update System
"""

__metadata__ = {
    'name': 'PO Approval Workflow',
    'version': '2.0.0',
    'author': 'Finance Operations',
    'tags': ['bpm', 'approval', 'workflow', 'po'],
    'dependencies': ['kinetic_devops.baq', 'kinetic_devops.base_client'],
    'usage_example': '''
from BPM.po_approval import route_po_for_approval, approve_po

# Route new PO
task = route_po_for_approval(po_number='PO-2024-001', amount=50000)

# Approve or reject
result = approve_po(po_number='PO-2024-001', approved=True)
    ''',
}

from typing import Dict, List, Any, Optional, Literal
from enum import Enum
from datetime import datetime


class ApprovalStatus(Enum):
    """Approval workflow status."""
    CREATED = 'created'
    PRE_SCREENING = 'pre_screening'
    AWAITING_APPROVAL = 'awaiting_approval'
    APPROVED = 'approved'
    REJECTED = 'rejected'
    CANCELLED = 'cancelled'


APPROVAL_RULES = {
    'low': {'threshold': 5000, 'approver_level': 'manager'},
    'medium': {'threshold': 25000, 'approver_level': 'director'},
    'high': {'threshold': 100000, 'approver_level': 'vp'},
}


def route_po_for_approval(
    po_number: str,
    amount: float,
    vendor_info: Optional[Dict] = None
) -> Dict[str, Any]:
    """
    Route a new PO through approval workflow.
    
    Args:
        po_number: Purchase order number
        amount: PO amount in dollars
        vendor_info: Optional vendor information dict
    
    Returns:
        Task routing information with assigned approver
    """
    
    # Determine approval level based on amount
    approval_level = 'low'
    if amount >= 100000:
        approval_level = 'high'
    elif amount >= 25000:
        approval_level = 'medium'
    
    rule = APPROVAL_RULES[approval_level]
    
    return {
        'success': True,
        'po_number': po_number,
        'status': ApprovalStatus.AWAITING_APPROVAL.value,
        'amount': amount,
        'approval_level': approval_level,
        'required_approver': rule['approver_level'],
        'routed_date': datetime.now().isoformat(),
        'message': f'PO routed to {rule["approver_level"]} for approval',
        'estimated_approval_date': '24_hours',
    }


def approve_po(
    po_number: str,
    approved: bool,
    approver_id: str = 'AUTO',
    comments: str = ''
) -> Dict[str, Any]:
    """
    Approve or reject a purchase order.
    
    Args:
        po_number: PO to approve/reject
        approved: True to approve, False to reject
        approver_id: User ID of approver
        comments: Approval comments or rejection reason
    
    Returns:
        Approval result with updated status
    """
    
    status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
    
    return {
        'success': True,
        'po_number': po_number,
        'status': status.value,
        'approver': approver_id,
        'approved_date': datetime.now().isoformat(),
        'comments': comments,
        'next_action': 'Send to procurement' if approved else 'Notify requestor of rejection',
        'audit_trail': {
            'action': 'approval_decision',
            'timestamp': datetime.now().isoformat(),
            'approver': approver_id,
            'decision': 'approved' if approved else 'rejected',
        },
    }


def get_approval_status(po_number: str) -> Dict[str, Any]:
    """Get current approval status of a PO."""
    return {
        'success': True,
        'po_number': po_number,
        'status': ApprovalStatus.AWAITING_APPROVAL.value,
        'approver': 'john.smith@company.com',
        'submitted_date': '2024-01-15T10:30:00Z',
        'hours_waiting': 5,
        'estimated_completion': '2024-01-15T18:00:00Z',
    }


def escalate_po(po_number: str, reason: str = '') -> Dict[str, Any]:
    """
    Escalate PO to higher approval level.
    
    Used when approval is delayed or special circumstances exist.
    """
    return {
        'success': True,
        'po_number': po_number,
        'escalated_to': 'VP Finance',
        'escalation_reason': reason,
        'escalated_date': datetime.now().isoformat(),
        'message': 'PO escalated to VP level approval',
    }


def get_pending_approvals(approver_id: str = 'ALL') -> Dict[str, Any]:
    """
    Get all pending approvals for an approver.
    
    Args:
        approver_id: Approver ID (ALL for system view)
    
    Returns:
        List of pending POs requiring approval
    """
    return {
        'success': True,
        'approver': approver_id,
        'pending_count': 12,
        'pending_orders': [
            {
                'po_number': 'PO-2024-001',
                'amount': 15000,
                'vendor': 'Acme Corp',
                'submitted': '2024-01-15T10:30:00Z',
                'hours_waiting': 5,
            },
            {
                'po_number': 'PO-2024-002',
                'amount': 45000,
                'vendor': 'Global Supplies',
                'submitted': '2024-01-14T14:15:00Z',
                'hours_waiting': 20,
            },
        ],
        'total_pending_amount': 450000.00,
    }
