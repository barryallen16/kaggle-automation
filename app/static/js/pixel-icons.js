// Pixel icon set - Pixelarticons v2.4.1 (MIT license, github.com/halfmoonui/Pixelarticons)
// Bundled locally as a drop-in replacement for lucide: any element
// <i data-lucide="name"></i> is replaced with the matching pixel-art SVG,
// preserving its Tailwind classes. Render refreshIcons() after DOM updates.
// Raw SVGs also live in /static/icons/pixel/ for easy swapping.
(function () {
  'use strict';

  var PIXEL_ICONS = {
  "activity": "<path d=\"M22 22H4v-2h18v2ZM4 20H2V2h2v18Zm4-6H6v-2h2v2Zm8 0h-2v-2h2v2Zm-6-2H8v-2h2v2Zm4 0h-2v-2h2v2Zm4 0h-2v-2h2v2Zm-6-2h-2V8h2v2Zm8 0h-2V8h2v2Zm2-2h-2V6h2v2Z\"/>",
  "alert-circle": "<path d=\"M4 2h16v2H4zm0 18h16v2H4zM20 4h2v16h-2zM2 4h2v16H2zm9 2h2v8h-2zm0 10h2v2h-2z\"/>",
  "alert-triangle": "<path d=\"M2 10h2v2H2zm0 4h2v-2H2zm20-4h-2v2h2zm0 4h-2v-2h2zM4 8h2v2H4zm0 8h2v-2H4zm16-8h-2v2h2zm0 8h-2v-2h2zM6 6h2v2H6zm0 12h2v-2H6zM18 6h-2v2h2zm0 12h-2v-2h2zM8 4h2v2H8zm0 16h2v-2H8zm8-16h-2v2h2zm0 16h-2v-2h2zM10 2h2v2h-2zm0 20h2v-2h-2zm4-20h-2v2h2zm0 20h-2v-2h2zm-3-5h2v-2h-2zm0-4h2V7h-2z\"/>",
  "archive": "<path d=\"M3 2h18v2H3zm0 5h18v2H3zM1 4h2v3H1zm20 0h2v3h-2zm-2 5h2v11h-2zM3 9h2v11H3zm2 11h14v2H5zm4-9h6v2H9z\"/>",
  "bell-ring": "<path d=\"M14 22H10V20H14V22ZM10 20H8V18H10V20ZM16 20H14V18H16V20ZM5 15H19V13H21V17H3V13H5V15ZM7 13H5V6H7V13ZM19 13H17V6H19V13ZM3 6H1V4H3V6ZM9 6H7V4H9V6ZM17 6H15V4H17V6ZM23 6H21V4H23V6ZM5 4H3V2H5V4ZM15 4H9V2H15V4ZM21 4H19V2H21V4Z\"/>",
  "check-circle-2": "<path d=\"M4 2h16v2H4zm0 18h16v2H4zM2 4h2v16H2zm18 0h2v16h-2zM7 12h2v2H7zm2 2h2v2H9zm2-2h2v2h-2zm2-2h2v2h-2zm2-2h2v2h-2z\"/>",
  "check-circle": "<path d=\"M10 18H8v-2h2v2Zm-2-2H6v-2h2v2Zm4-2v2h-2v-2h2Zm-6 0H4v-2h2v2Zm8 0h-2v-2h2v2Zm2-2h-2v-2h2v2Zm2-2h-2V8h2v2Zm2-2h-2V6h2v2Z\"/>",
  "clock": "<path d=\"M6 2h12v2H6zM2 6h2v12H2zm18 0h2v12h-2zm-2-2h2v2h-2zM4 4h2v2H4zm2 18h12v-2H6zm12-2h2v-2h-2zM4 20h2v-2H4zm7-14h2v7h-2zm2 7h2v2h-2zm2 2h2v2h-2z\"/>",
  "cloud-download": "<path d=\"M20 6h-2v2h2V6Zm2 2h-2v4h2V8Zm-2 4H4v2h16v-2ZM4 8H2v4h2V8Zm4-2H4v2h4V6Zm8-4h-6v2h6V2Zm-6 2H8v2h2V4Zm0 4H8v2h2V8Zm8-4h-2v2h2V4Zm0 4h-2v2h2V8Zm-7 8h2v2h-2zm0 4h2v2h-2zm-7-2h7v2H4zm9 0h7v2h-7zm-2-4h2v2h-2z\"/>",
  "cpu": "<path d=\"M5 3h14v2H5zm0 16h14v2H5zM3 5h2v14H3zm16 0h2v14h-2zM9 7h6v2H9zm0 8h6v2H9zM7 9h2v6H7zm8 0h2v6h-2zm-4-8h2v2h-2zm0 20h2v2h-2zM1 11h2v2H1zm20 0h2v2h-2zm0-4h2v2h-2zm0 8h2v2h-2zM1 15h2v2H1zm0-8h2v2H1zm6-6h2v2H7zm8 0h2v2h-2zm0 20h2v2h-2zm-8 0h2v2H7z\"/>",
  "download-cloud": "<path d=\"M21 15v4h-2v-4zm-2 4v2H5v-2zM5 15v4H3v-4zm8-12v14h-2V3z\"/> <path d=\"M7 11v2h10v-2zm2 2v2h2v-2zm4 0v2h2v-2z\"/> <path d=\"M15 11v2h2v-2z\"/>",
  "download": "<path d=\"M21 15v4h-2v-4zm-2 4v2H5v-2zM5 15v4H3v-4zm8-12v14h-2V3z\"/> <path d=\"M7 11v2h10v-2zm2 2v2h2v-2zm4 0v2h2v-2z\"/> <path d=\"M15 11v2h2v-2z\"/>",
  "external-link": "<path d=\"M11 5H5v2h6V5ZM5 7H3v12h2V7Zm12 12H5v2h12v-2Zm2-6h-2v6h2v-6Zm-8 0H9v2h2v-2Zm2-2h-2v2h2v-2Zm2-2h-2v2h2V9Zm2-2h-2v2h2V7Zm2-2h-2v2h2V5Zm2-2h-2v8h2V3Z\"/> <path d=\"M21 3h-8v2h8V3Z\"/>",
  "file": "<path d=\"M6 4H4v16h2zm10-2H6v2h10zm4 4h-2v14h2zm-2 14H6v2h12zM16 4h2v2h-2zm-4 0h2v6h-2z\"/> <path d=\"M12 8h6v2h-6z\"/>",
  "folder-down": "<path d=\"M4 4h6v2H4zm0 14h16v2H4zM20 8h2v10h-2zM2 6h2v12H2zm8 0h10v2H10z\"/>",
  "history": "<path d=\"M6 2h12v2H6zM2 6h2v12H2zm18 0h2v12h-2zm-2-2h2v2h-2zM4 4h2v2H4zm2 18h12v-2H6zm12-2h2v-2h-2zM4 20h2v-2H4zm7-14h2v7h-2zm2 7h2v2h-2zm2 2h2v2h-2z\"/>",
  "info": "<path d=\"M18 22H6V20H18V22ZM6 20H4V18H6V20ZM20 20H18V18H20V20ZM4 18H2V6H4V18ZM22 18H20V6H22V18ZM13 17H11V11H13V17ZM13 9H11V7H13V9ZM6 6H4V4H6V6ZM20 6H18V4H20V6ZM18 4H6V2H18V4Z\"/>",
  "layout-dashboard": "<path d=\"M4 2h16v2H4zM2 4h2v16H2zm2 7h16v2H4zm16-7h2v16h-2z\"/> <path d=\"M11 4h2v18h-2z\"/> <path d=\"M4 20h16v2H4z\"/>",
  "loader-2": "<path d=\"M13 22h-2v-6h2v6Zm-6-3H5v-2h2v2Zm12 0h-2v-2h2v2ZM9 17H7v-2h2v2Zm8 0h-2v-2h2v2Zm-9-4H2v-2h6v2Zm14 0h-6v-2h6v2ZM9 9H7V7h2v2Zm8 0h-2V7h2v2Zm-4-1h-2V2h2v6ZM7 7H5V5h2v2Zm12 0h-2V5h2v2Z\"/>",
  "lock": "<path d=\"M5 8h14v2H5zm0 12h14v2H5zM3 10h2v10H3zm16 0h2v10h-2zM7 4h2v4H7zm2-2h6v2H9zm6 2h2v4h-2z\"/>",
  "log-out": "<path d=\"M8 11h12v2H8zm8-2h2v2h-2z\"/> <path d=\"M14 7h2v10h-2zm2 6h2v2h-2zM6 2h12v2H6zm0 18h12v2H6zM4 4h2v16H4zm14 0h2v3h-2zm0 13h2v3h-2z\"/>",
  "network": "<path d=\"M20 22H4V20H20V22ZM4 20H2V14H4V20ZM22 20H20V14H22V20ZM13 4H15V6H17V8H13V18H11V8H7V6H9V4H11V2H13V4ZM9 14H4V12H9V14ZM20 14H15V12H20V14Z\"/>",
  "play-circle": "<path d=\"M15 11h-2V9h2zm0 4h-2v-2h2zm-2 2h-2v-2h2zm0-8h-2V7h2zm-2-2H9V5h2zM9 21H7V3h2zm6-8h2v-2h-2zm-6 4h2v2H9z\"/>",
  "play": "<path d=\"M15 11h-2V9h2zm0 4h-2v-2h2zm-2 2h-2v-2h2zm0-8h-2V7h2zm-2-2H9V5h2zM9 21H7V3h2zm6-8h2v-2h-2zm-6 4h2v2H9z\"/>",
  "plus": "<path d=\"M13 11h7v2h-7v7h-2v-7H4v-2h7V4h2v7Z\"/>",
  "radio": "<path d=\"M11 9h2v2h-2zm0 4h2v2h-2zm-2-2h2v2H9zm4 0h2v2h-2zm6-2h-2v6h2zM5 9h2v6H5zm18-2h-2v10h2zM1 7h2v10H1zm16 0h-2v2h2zM7 7h2v2H7zm14-2h-2v2h2zM3 5h2v2H3zm14 10h-2v2h2zM7 15h2v2H7zm14 2h-2v2h2zM3 17h2v2H3z\"/>",
  "refresh-cw": "<path d=\"M13 20H9V18H13V20ZM19 16H21V18H19V20H17V18H15V16H17V8H19V16ZM9 18H7V16H9V18ZM7 6H9V8H7V16H5V8H3V6H5V4H7V6ZM15 16H13V14H15V16ZM23 16H21V14H23V16ZM3 10H1V8H3V10ZM11 10H9V8H11V10ZM17 8H15V6H17V8ZM15 6H11V4H15V6Z\"/>",
  "rocket": "<path d=\"M4 13h8v6h2v2h-2v2h-2v-8H2v-4h2v2Zm12 6h-2v-2h2v2Zm2-2h-2v-2h2v2Zm2-2h-2v-2h2v2Zm-6-6h8v4h-2v-2h-8V5h-2V3h2V1h2v8Zm-8 2H4V9h2v2Zm2-2H6V7h2v2Zm2-2H8V5h2v2Z\"/>",
  "rotate-cw": "<path d=\"M16 4h2v6h-2zm-2-2h2v2h-2zm0 2h2v8h-2zM4 8H2v5h2z\"/> <path d=\"M4 6h16v2H4zm4 14H6v-6h2zm2 2H8v-2h2zm0-2H8v-8h2zm10-4h2v-5h-2z\"/> <path d=\"M20 18H4v-2h16z\"/>",
  "save": "<path d=\"M20 22H4V20H6V14H8V20H16V14H18V20H20V22ZM4 20H2V4H4V20ZM22 20H20V6H22V20ZM16 14H8V12H16V14ZM12 10H6V6H12V10ZM20 6H18V4H20V6ZM18 4H4V2H18V4Z\"/>",
  "send": "<path d=\"M4 19h4v2H2v-8h2v6Zm8 0H8v-2h4v2Zm4-2h-4v-2h4v2Zm4-2h-4v-2h4v2Zm-10-2H4v-2h6v2Zm12 0h-2v-2h2v2ZM8 5H4v6H2V3h6v2Zm12 6h-4V9h4v2Zm-4-2h-4V7h4v2Zm-4-2H8V5h4v2Z\"/>",
  "shield-check": "<path d=\"M4 2h16v2H4zM2 4h2v10H2zm18 0h2v10h-2zM4 14h2v2H4zm2 2h2v2H6zm4 4h4v2h-4zm10-6h-2v2h2zm-2 2h-2v2h2zm-2 2h-2v2h2zm-6 0H8v2h2z\"/>",
  "terminal": "<path d=\"M4 2h16v2H4zm0 18h16v2H4zM2 4h2v16H2zm18 0h2v16h-2zM6 16h2v2H6zm2-2h2v2H8zm-2-2h2v2H6z\"/>",
  "trash-2": "<path d=\"M18 22H6V20H18V22ZM9 6H15V4H17V6H22V8H20V20H18V8H6V20H4V8H2V6H7V4H9V6ZM15 4H9V2H15V4Z\"/>",
  "trash": "<path d=\"M18 22H6V20H18V22ZM9 6H15V4H17V6H22V8H20V20H18V8H6V20H4V8H2V6H7V4H9V6ZM15 4H9V2H15V4Z\"/>",
  "upload-cloud": "<path d=\"M19 21H5v-2h14v2ZM5 19H3v-4h2v4Zm16 0h-2v-4h2v4ZM13 5h2v2h2v2h-4v8h-2V9H7V7h2V5h2V3h2v2Z\"/>",
  "user-plus": "<path d=\"M9 2h6v2H9zm0 8h6v2H9zm6-6h2v6h-2zM7 4h2v6H7zM4 18h2v4H4zm14 0h2v4h-2zM8 14h8v2H8zm-2 2h2v2H6z\"/> <path d=\"M18 16h2v6h-2z\"/> <path d=\"M16 18h6v2h-6z\"/>",
  "user-x": "<path d=\"M9 2h6v2H9zm0 8h6v2H9zm6-6h2v6h-2zM7 4h2v6H7zM4 18h2v4H4zm16 2h2v2h-2zM8 14h6v2H8zm-2 2h2v2H6zm10 0h2v2h-2zm2 2h2v2h-2zm2-2h2v2h-2zm-4 4h2v2h-2z\"/>",
  "users": "<path d=\"M5 2h6v2H5zm10 0h4v2h-4zM5 10h6v2H5zm10 0h4v2h-4zm4-6h2v6h-2zm-8 0h2v6h-2zM3 4h2v6H3zM0 18h2v4H0zm14 0h2v4h-2zm8 0h2v4h-2zM4 14h8v2H4zm12 0h4v2h-4zM2 16h2v2H2zm10 0h2v2h-2zm8 0h2v2h-2z\"/>",
  "x": "<path d=\"M6 20H4v-2h2v2Zm14-2h2v2h-6v-2h2v-2h2v2ZM8 18H6v-2h2v2Zm8 0h-2v-2h2v2Zm-6-2H8v-2h2v2Zm4 0h-2v-2h2v2Zm4 0h-2v-2h2v2Zm-6-2h-2v-2h2v2Zm4 0h-2v-2h2v2Zm-6-2H8v-2h2v2Zm4 0h-2v-2h2v2Zm-6-2H6V8h2v2Zm4 0h-2V8h2v2Zm4 0h-2V8h2v2ZM8 6H6v2H4V6H2V4h6v2Zm2 2H8V6h2v2Zm8 0h-2V6h2v2Zm2-2h-2V4h2v2Z\"/>",
  "zap": "<path d=\"M4 13h8v6h2v2h-2v2h-2v-8H2v-4h2v2Zm12 6h-2v-2h2v2Zm2-2h-2v-2h2v2Zm2-2h-2v-2h2v2Zm-6-6h8v4h-2v-2h-8V5h-2V3h2V1h2v8Zm-8 2H4V9h2v2Zm2-2H6V7h2v2Zm2-2H8V5h2v2Z\"/>"
};

  function iconSvg(name, cls) {
    var body = PIXEL_ICONS[name];
    if (!body) return '';
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"' +
      ' shape-rendering="crispEdges" aria-hidden="true" focusable="false"' +
      ' class="' + (cls || '') + '">' + body + '</svg>';
  }

  window.refreshIcons = function (root) {
    var scope = root || document;
    scope.querySelectorAll('i[data-lucide]').forEach(function (el) {
      var name = el.getAttribute('data-lucide');
      var svg = iconSvg(name, el.getAttribute('class'));
      if (!svg) {
        el.style.display = 'none';
        return;
      }
      el.outerHTML = svg;
    });
  };

  window.pixelIconSvg = iconSvg;
})();
