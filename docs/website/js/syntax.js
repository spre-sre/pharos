(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    // Inject syntax highlight styles once
    var styleId = 'syntax-highlight-styles';
    if (!document.getElementById(styleId)) {
      var style = document.createElement('style');
      style.id = styleId;
      style.textContent = [
        '.syn-keyword { color: var(--accent-cyan, #00e5ff); }',
        '.syn-string { color: var(--accent-green, #69f0ae); }',
        '.syn-number { color: var(--accent-amber, #ffd740); }',
        '.syn-comment { color: var(--text-tertiary, #6b7280); }',
        '.syn-property { color: var(--accent-cyan, #00e5ff); }',
        '.syn-flag { color: var(--accent-purple, #b388ff); }',
        '.syn-boolean { color: var(--accent-purple, #b388ff); }'
      ].join('\n');
      document.head.appendChild(style);
    }

    var codeElements = document.querySelectorAll('.code-block-body code');

    codeElements.forEach(function (codeEl) {
      var codeBlock = codeEl.closest('.code-block[data-language]');
      if (!codeBlock) return;

      var language = codeBlock.getAttribute('data-language').toLowerCase();
      var raw = codeEl.textContent;

      if (language === 'json') {
        codeEl.innerHTML = highlightJSON(raw);
      } else if (language === 'bash' || language === 'shell' || language === 'sh') {
        codeEl.innerHTML = highlightBash(raw);
      }
    });

    function escapeHTML(str) {
      return str
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    function highlightJSON(text) {
      var result = '';
      var i = 0;
      var len = text.length;

      while (i < len) {
        var ch = text[i];

        // Strings
        if (ch === '"') {
          var str = readString(text, i);
          var escaped = escapeHTML(str);

          // Check if this is a property key (followed by colon)
          var afterStr = i + str.length;
          var rest = text.substring(afterStr).replace(/^\s*/, '');
          if (rest[0] === ':') {
            result += '<span class="syn-property">' + escaped + '</span>';
          } else {
            result += '<span class="syn-string">' + escaped + '</span>';
          }
          i += str.length;
          continue;
        }

        // Numbers
        if ((ch >= '0' && ch <= '9') || (ch === '-' && i + 1 < len && text[i + 1] >= '0' && text[i + 1] <= '9')) {
          var numStr = '';
          if (ch === '-') {
            numStr += ch;
            i++;
          }
          while (i < len && ((text[i] >= '0' && text[i] <= '9') || text[i] === '.' || text[i] === 'e' || text[i] === 'E' || text[i] === '+' || text[i] === '-')) {
            numStr += text[i];
            i++;
          }
          result += '<span class="syn-number">' + escapeHTML(numStr) + '</span>';
          continue;
        }

        // Booleans and null
        if (text.substring(i, i + 4) === 'true') {
          result += '<span class="syn-boolean">true</span>';
          i += 4;
          continue;
        }
        if (text.substring(i, i + 5) === 'false') {
          result += '<span class="syn-boolean">false</span>';
          i += 5;
          continue;
        }
        if (text.substring(i, i + 4) === 'null') {
          result += '<span class="syn-boolean">null</span>';
          i += 4;
          continue;
        }

        result += escapeHTML(ch);
        i++;
      }

      return result;
    }

    function readString(text, start) {
      var i = start + 1;
      var str = '"';
      while (i < text.length) {
        var ch = text[i];
        str += ch;
        if (ch === '\\') {
          i++;
          if (i < text.length) {
            str += text[i];
          }
        } else if (ch === '"') {
          break;
        }
        i++;
      }
      return str;
    }

    function highlightBash(text) {
      var lines = text.split('\n');
      var commands = ['git', 'uv', 'pip', 'python', 'mcpm', 'podman', 'docker', 'claude', 'lumino', 'curl', 'wget', 'npm', 'npx', 'cd', 'ls', 'mkdir', 'cp', 'mv', 'rm', 'chmod', 'chown', 'export', 'source', 'sudo'];

      var highlighted = lines.map(function (line) {
        // Comment lines
        var trimmed = line.trimStart();
        if (trimmed.startsWith('#')) {
          return '<span class="syn-comment">' + escapeHTML(line) + '</span>';
        }

        var result = '';
        var tokens = tokenizeBash(line);

        tokens.forEach(function (token) {
          if (token.type === 'string') {
            result += '<span class="syn-string">' + escapeHTML(token.value) + '</span>';
          } else if (token.type === 'comment') {
            result += '<span class="syn-comment">' + escapeHTML(token.value) + '</span>';
          } else if (token.type === 'flag') {
            result += '<span class="syn-flag">' + escapeHTML(token.value) + '</span>';
          } else if (token.type === 'command') {
            result += '<span class="syn-keyword">' + escapeHTML(token.value) + '</span>';
          } else {
            result += escapeHTML(token.value);
          }
        });

        return result;
      });

      return highlighted.join('\n');
    }

    function tokenizeBash(line) {
      var tokens = [];
      var i = 0;
      var len = line.length;
      var commandNames = ['git', 'uv', 'pip', 'python', 'mcpm', 'podman', 'docker', 'claude', 'lumino', 'curl', 'wget', 'npm', 'npx', 'cd', 'ls', 'mkdir', 'cp', 'mv', 'rm', 'chmod', 'chown', 'export', 'source', 'sudo'];

      while (i < len) {
        var ch = line[i];

        // Inline comment (space followed by #)
        if (ch === '#' && (i === 0 || line[i - 1] === ' ')) {
          tokens.push({ type: 'comment', value: line.substring(i) });
          break;
        }

        // Quoted strings
        if (ch === '"' || ch === "'") {
          var quote = ch;
          var str = ch;
          i++;
          while (i < len && line[i] !== quote) {
            if (line[i] === '\\' && quote === '"') {
              str += line[i];
              i++;
              if (i < len) {
                str += line[i];
                i++;
              }
            } else {
              str += line[i];
              i++;
            }
          }
          if (i < len) {
            str += line[i];
            i++;
          }
          tokens.push({ type: 'string', value: str });
          continue;
        }

        // Flags --something or -x
        if (ch === '-' && i + 1 < len && line[i + 1] !== ' ') {
          var flag = '';
          while (i < len && line[i] !== ' ' && line[i] !== '=') {
            flag += line[i];
            i++;
          }
          // Include = and value if present
          if (i < len && line[i] === '=') {
            flag += line[i];
            i++;
          }
          tokens.push({ type: 'flag', value: flag });
          continue;
        }

        // Words
        if (ch !== ' ') {
          var word = '';
          while (i < len && line[i] !== ' ') {
            word += line[i];
            i++;
          }

          // Check if word is a known command
          var bareWord = word.replace(/^\$\s*/, '');
          if (commandNames.indexOf(bareWord) !== -1) {
            tokens.push({ type: 'command', value: word });
          } else {
            tokens.push({ type: 'text', value: word });
          }
          continue;
        }

        // Whitespace
        var ws = '';
        while (i < len && line[i] === ' ') {
          ws += line[i];
          i++;
        }
        tokens.push({ type: 'text', value: ws });
      }

      return tokens;
    }
  });
})();
