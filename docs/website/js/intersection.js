(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    var revealElements = document.querySelectorAll('.reveal');

    if (revealElements.length === 0) return;

    // Respect prefers-reduced-motion
    var prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

    if (prefersReducedMotion.matches) {
      revealElements.forEach(function (el) {
        el.classList.add('visible');
      });
      return;
    }

    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('visible');
          observer.unobserve(entry.target);
        }
      });
    }, {
      threshold: 0.1,
      rootMargin: '-50px'
    });

    revealElements.forEach(function (el) {
      observer.observe(el);
    });
  });
})();
