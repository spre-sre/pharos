(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    var toggleButton = document.querySelector('.mobile-nav-toggle');
    var overlay = document.querySelector('.mobile-nav-overlay');

    if (!toggleButton || !overlay) return;

    function openNav() {
      overlay.classList.add('open');
      toggleButton.classList.add('open');
      toggleButton.setAttribute('aria-expanded', 'true');
      document.body.classList.add('no-scroll');
    }

    function closeNav() {
      overlay.classList.remove('open');
      toggleButton.classList.remove('open');
      toggleButton.setAttribute('aria-expanded', 'false');
      document.body.classList.remove('no-scroll');
    }

    function isOpen() {
      return overlay.classList.contains('open');
    }

    // Toggle on button click
    toggleButton.addEventListener('click', function () {
      if (isOpen()) {
        closeNav();
      } else {
        openNav();
      }
    });

    // Close on nav link click
    var navLinks = overlay.querySelectorAll('a');
    navLinks.forEach(function (link) {
      link.addEventListener('click', function () {
        closeNav();
      });
    });

    // Close on Escape key
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && isOpen()) {
        closeNav();
        toggleButton.focus();
      }
    });

    // Close on overlay background click (if clicking the overlay itself, not its children)
    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) {
        closeNav();
      }
    });
  });
})();
