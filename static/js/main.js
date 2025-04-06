// RSSHub Admin & Quality Control System

// Initialize tooltips
document.addEventListener('DOMContentLoaded', function() {
    // Bootstrap 5 tooltip initialization
    var tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    var tooltipList = tooltipTriggerList.map(function (tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl);
    });
    
    // Handle custom selectors in JSON form
    var customSelectorsField = document.getElementById('custom_selectors');
    if (customSelectorsField) {
        customSelectorsField.addEventListener('blur', function() {
            try {
                // If field is not empty, try to parse JSON
                if (this.value.trim()) {
                    const parsed = JSON.parse(this.value);
                    // Format with indentation
                    this.value = JSON.stringify(parsed, null, 2);
                }
            } catch (e) {
                // If JSON is invalid, just leave as is - form validation will catch it
            }
        });
    }
    
    // Auto-refresh data
    setupAutoRefresh();
});

// Function to setup auto-refresh for dashboard data
function setupAutoRefresh() {
    // Check if we're on the dashboard
    if (window.location.pathname === '/' || window.location.pathname === '/dashboard') {
        // Refresh every 5 minutes
        setTimeout(function() {
            window.location.reload();
        }, 5 * 60 * 1000);
    }
}

// Function to format quality scores with colors
function formatQualityScore(score) {
    let colorClass = 'quality-low';
    if (score >= 80) {
        colorClass = 'quality-high';
    } else if (score >= 50) {
        colorClass = 'quality-medium';
    }
    
    return `<span class="${colorClass}">${Math.round(score)}%</span>`;
}

// Function to toggle feed active status
function toggleFeedStatus(feedId, currentStatus) {
    fetch(`/api/feed/toggle-status/${feedId}`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ status: !currentStatus })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            // Update UI
            const statusBadge = document.getElementById(`status-badge-${feedId}`);
            if (statusBadge) {
                if (data.is_active) {
                    statusBadge.className = 'badge bg-success';
                    statusBadge.textContent = 'Active';
                } else {
                    statusBadge.className = 'badge bg-secondary';
                    statusBadge.textContent = 'Inactive';
                }
            }
            
            // Show success message
            showAlert(`Feed status ${data.is_active ? 'activated' : 'deactivated'}`, 'success');
        } else {
            showAlert('Failed to update status', 'danger');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showAlert('Error updating status', 'danger');
    });
}

// Function to show alert message
function showAlert(message, type = 'info') {
    const alertContainer = document.createElement('div');
    alertContainer.className = `alert alert-${type} alert-dismissible fade show`;
    alertContainer.setAttribute('role', 'alert');
    
    alertContainer.innerHTML = `
        ${message}
        <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
    `;
    
    // Insert at the top of the main container
    const container = document.querySelector('.container');
    container.insertBefore(alertContainer, container.firstChild);
    
    // Auto-dismiss after 5 seconds
    setTimeout(() => {
        const bsAlert = new bootstrap.Alert(alertContainer);
        bsAlert.close();
    }, 5000);
}

// Function to copy RSSHub URL to clipboard
function copyRSSHubUrl(route) {
    // Get RSSHub base URL from the page
    const baseUrl = document.getElementById('rsshub-base-url')?.textContent || 'http://localhost:1200/';
    const fullUrl = baseUrl + route.trim();
    
    navigator.clipboard.writeText(fullUrl)
        .then(() => {
            showAlert('RSSHub URL copied to clipboard', 'success');
        })
        .catch(err => {
            console.error('Could not copy text: ', err);
            showAlert('Failed to copy URL', 'danger');
        });
}