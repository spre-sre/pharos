(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    var header = document.querySelector('.sticky-header');
    var headerHeight = header ? header.offsetHeight : 0;

    // --- Smooth scroll for anchor links ---
    document.addEventListener('click', function (e) {
      var link = e.target.closest('a[href^="#"]');
      if (!link) return;

      var targetId = link.getAttribute('href');
      if (targetId === '#' || targetId.length < 2) return;

      var target = document.querySelector(targetId);
      if (!target) return;

      e.preventDefault();

      var targetPosition = target.getBoundingClientRect().top + window.scrollY - headerHeight;
      window.scrollTo({ top: targetPosition, behavior: 'smooth' });
    });

    // --- Sticky header scroll class ---
    if (header) {
      function updateHeaderState() {
        if (window.scrollY > 50) {
          header.classList.add('scrolled');
        } else {
          header.classList.remove('scrolled');
        }
      }

      window.addEventListener('scroll', updateHeaderState, { passive: true });
      updateHeaderState();
    }

    // --- Active nav highlighting via IntersectionObserver ---
    var sections = document.querySelectorAll('section[id]');
    var navLinks = document.querySelectorAll('.nav-link[href^="#"]');

    if (sections.length > 0 && navLinks.length > 0) {
      var observerOptions = {
        root: null,
        rootMargin: '-' + (headerHeight + 20) + 'px 0px -40% 0px',
        threshold: 0
      };

      var sectionObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            var id = entry.target.getAttribute('id');
            navLinks.forEach(function (link) {
              link.classList.remove('active');
              if (link.getAttribute('href') === '#' + id) {
                link.classList.add('active');
              }
            });
          }
        });
      }, observerOptions);

      sections.forEach(function (section) {
        sectionObserver.observe(section);
      });
    }

    // --- Close mobile nav when a nav link is clicked ---
    var mobileOverlay = document.querySelector('.mobile-nav-overlay');
    var mobileToggle = document.querySelector('.mobile-nav-toggle');

    if (mobileOverlay) {
      var mobileNavLinks = mobileOverlay.querySelectorAll('a');
      mobileNavLinks.forEach(function (link) {
        link.addEventListener('click', function () {
          mobileOverlay.classList.remove('open');
          document.body.classList.remove('no-scroll');
          if (mobileToggle) {
            mobileToggle.classList.remove('open');
            mobileToggle.setAttribute('aria-expanded', 'false');
          }
        });
      });
    }
  });
})();
