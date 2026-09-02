(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    var tabContainers = document.querySelectorAll('.tabs');

    tabContainers.forEach(function (container) {
      var tabList = container.querySelector('[role="tablist"]');
      var buttons = container.querySelectorAll('.tab-button');
      var panels = container.querySelectorAll('[role="tabpanel"]');

      // Ensure ARIA roles are set
      if (tabList) {
        tabList.setAttribute('role', 'tablist');
      }

      buttons.forEach(function (button, index) {
        button.setAttribute('role', 'tab');

        var tabId = button.getAttribute('data-tab');
        var panelId = tabId + '-panel';
        var buttonId = tabId + '-tab';

        button.setAttribute('id', buttonId);
        button.setAttribute('aria-controls', panelId);
        button.setAttribute('tabindex', button.classList.contains('active') ? '0' : '-1');
        button.setAttribute('aria-selected', button.classList.contains('active') ? 'true' : 'false');

        // Find corresponding panel
        var panel = container.querySelector('[data-tab-panel="' + tabId + '"]') ||
                    panels[index];

        if (panel) {
          panel.setAttribute('role', 'tabpanel');
          panel.setAttribute('id', panelId);
          panel.setAttribute('aria-labelledby', buttonId);

          if (!button.classList.contains('active')) {
            panel.setAttribute('hidden', '');
          } else {
            panel.removeAttribute('hidden');
          }
        }
      });

      // Click handler
      buttons.forEach(function (button) {
        button.addEventListener('click', function () {
          activateTab(container, button, buttons, panels);
        });
      });

      // Keyboard navigation
      if (tabList) {
        tabList.addEventListener('keydown', function (e) {
          var currentIndex = Array.from(buttons).indexOf(document.activeElement);
          if (currentIndex === -1) return;

          var newIndex = currentIndex;
          var len = buttons.length;

          switch (e.key) {
            case 'ArrowRight':
              e.preventDefault();
              newIndex = (currentIndex + 1) % len;
              break;
            case 'ArrowLeft':
              e.preventDefault();
              newIndex = (currentIndex - 1 + len) % len;
              break;
            case 'Home':
              e.preventDefault();
              newIndex = 0;
              break;
            case 'End':
              e.preventDefault();
              newIndex = len - 1;
              break;
            default:
              return;
          }

          buttons[newIndex].focus();
          activateTab(container, buttons[newIndex], buttons, panels);
        });
      }
    });

    function activateTab(container, activeButton, buttons, panels) {
      var activeTabId = activeButton.getAttribute('data-tab');

      // Deactivate all
      buttons.forEach(function (btn) {
        btn.classList.remove('active');
        btn.setAttribute('aria-selected', 'false');
        btn.setAttribute('tabindex', '-1');
      });

      panels.forEach(function (panel) {
        panel.setAttribute('hidden', '');
      });

      // Activate selected
      activeButton.classList.add('active');
      activeButton.setAttribute('aria-selected', 'true');
      activeButton.setAttribute('tabindex', '0');

      // Show corresponding panel
      var targetPanel = container.querySelector('[data-tab-panel="' + activeTabId + '"]') ||
                        document.getElementById(activeTabId + '-panel');

      if (targetPanel) {
        targetPanel.removeAttribute('hidden');
      }
    }
  });
})();
