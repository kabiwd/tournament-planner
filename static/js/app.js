// Auto-dismiss flash messages after 4 seconds
document.addEventListener('DOMContentLoaded', function () {
    const flashes = document.querySelectorAll('.flash');
    flashes.forEach(function (flash) {
        setTimeout(function () {
            flash.style.opacity = '0';
            flash.style.transform = 'translateY(-8px)';
            flash.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
            setTimeout(function () { flash.remove(); }, 300);
        }, 4000);
    });

    // Highlight active tournament nav link
    const currentPath = window.location.pathname;
    document.querySelectorAll('.t-nav-link').forEach(function (link) {
        if (link.getAttribute('href') === currentPath) {
            link.style.color = '#e8e8f0';
            link.style.background = 'rgba(255,255,255,0.06)';
        }
    });
});
