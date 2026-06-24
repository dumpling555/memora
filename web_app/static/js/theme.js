const themeManager = {
  _theme: 'light',

  getTheme() {
    return this._theme;
  },

  setTheme(theme) {
    this._theme = theme;
    localStorage.setItem('kilo_theme', theme);
    this.apply();
  },

  toggle() {
    this.setTheme(this._theme === 'light' ? 'dark' : 'light');
  },

  apply() {
    if (this._theme === 'dark') {
      document.documentElement.classList.add('dark');
    } else {
      document.documentElement.classList.remove('dark');
    }
    document.querySelectorAll('[data-i18n-theme]').forEach(el => {
      const key = el.dataset.i18nTheme;
      el.textContent = i18n.get(key);
    });
  },

  init() {
    const saved = localStorage.getItem('kilo_theme');
    if (saved === 'dark' || (!saved && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
      this._theme = 'dark';
    }
    this.apply();
  }
};
