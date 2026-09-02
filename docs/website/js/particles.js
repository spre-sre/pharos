(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    var canvas = document.getElementById('hero-canvas');
    if (!canvas) return;

    var ctx = canvas.getContext('2d');
    if (!ctx) return;

    var prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    var mouse = { x: null, y: null };
    var particles = [];
    var animationId = null;
    var LINE_DISTANCE = 150;
    var MOUSE_RADIUS = 200;

    function getParticleCount() {
      return canvas.width < 768 ? 30 : 80;
    }

    function resizeCanvas() {
      var rect = canvas.parentElement.getBoundingClientRect();
      canvas.width = rect.width;
      canvas.height = rect.height;
    }

    function createParticle() {
      return {
        x: Math.random() * canvas.width,
        y: Math.random() * canvas.height,
        radius: 1 + Math.random(),
        alpha: 0.3 + Math.random() * 0.3,
        vx: (Math.random() - 0.5) * 0.5,
        vy: (Math.random() - 0.5) * 0.5
      };
    }

    function initParticles() {
      particles = [];
      var count = getParticleCount();
      for (var i = 0; i < count; i++) {
        particles.push(createParticle());
      }
    }

    function drawParticle(p) {
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.radius, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(0, 229, 255, ' + p.alpha + ')';
      ctx.fill();
    }

    function drawLine(p1, p2, dist) {
      var opacity = 0.1 * (1 - dist / LINE_DISTANCE);
      ctx.beginPath();
      ctx.moveTo(p1.x, p1.y);
      ctx.lineTo(p2.x, p2.y);
      ctx.strokeStyle = 'rgba(0, 229, 255, ' + opacity + ')';
      ctx.lineWidth = 0.5;
      ctx.stroke();
    }

    function update() {
      for (var i = 0; i < particles.length; i++) {
        var p = particles[i];

        // Mouse interaction
        if (mouse.x !== null && mouse.y !== null) {
          var dx = p.x - mouse.x;
          var dy = p.y - mouse.y;
          var dist = Math.sqrt(dx * dx + dy * dy);

          if (dist < MOUSE_RADIUS && dist > 0) {
            var force = (MOUSE_RADIUS - dist) / MOUSE_RADIUS;
            p.vx += (dx / dist) * force * 0.2;
            p.vy += (dy / dist) * force * 0.2;
          }
        }

        // Apply velocity with damping
        p.x += p.vx;
        p.y += p.vy;
        p.vx *= 0.99;
        p.vy *= 0.99;

        // Wrap around edges
        if (p.x < -10) p.x = canvas.width + 10;
        if (p.x > canvas.width + 10) p.x = -10;
        if (p.y < -10) p.y = canvas.height + 10;
        if (p.y > canvas.height + 10) p.y = -10;
      }
    }

    function draw() {
      ctx.clearRect(0, 0, canvas.width, canvas.height);

      // Draw lines
      for (var i = 0; i < particles.length; i++) {
        for (var j = i + 1; j < particles.length; j++) {
          var dx = particles[i].x - particles[j].x;
          var dy = particles[i].y - particles[j].y;
          var dist = Math.sqrt(dx * dx + dy * dy);

          if (dist < LINE_DISTANCE) {
            drawLine(particles[i], particles[j], dist);
          }
        }
      }

      // Draw particles
      for (var k = 0; k < particles.length; k++) {
        drawParticle(particles[k]);
      }
    }

    function animate() {
      update();
      draw();
      animationId = requestAnimationFrame(animate);
    }

    function drawStatic() {
      resizeCanvas();
      initParticles();
      draw();
    }

    // Mouse tracking
    canvas.parentElement.addEventListener('mousemove', function (e) {
      var rect = canvas.getBoundingClientRect();
      mouse.x = e.clientX - rect.left;
      mouse.y = e.clientY - rect.top;
    });

    canvas.parentElement.addEventListener('mouseleave', function () {
      mouse.x = null;
      mouse.y = null;
    });

    // Resize handler
    var resizeTimeout;
    window.addEventListener('resize', function () {
      clearTimeout(resizeTimeout);
      resizeTimeout = setTimeout(function () {
        resizeCanvas();
        // Reinitialize if particle count changes significantly
        var targetCount = getParticleCount();
        if (Math.abs(particles.length - targetCount) > 10) {
          initParticles();
        }
        if (prefersReducedMotion.matches) {
          draw();
        }
      }, 200);
    });

    // Initialize
    resizeCanvas();
    initParticles();

    if (prefersReducedMotion.matches) {
      // Static render for reduced motion
      draw();

      // Listen for changes in motion preference
      prefersReducedMotion.addEventListener('change', function (e) {
        if (e.matches) {
          if (animationId) {
            cancelAnimationFrame(animationId);
            animationId = null;
          }
          draw();
        } else {
          animate();
        }
      });
    } else {
      animate();

      prefersReducedMotion.addEventListener('change', function (e) {
        if (e.matches) {
          if (animationId) {
            cancelAnimationFrame(animationId);
            animationId = null;
          }
          draw();
        } else {
          animate();
        }
      });
    }
  });
})();
