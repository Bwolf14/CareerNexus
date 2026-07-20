// Alert form: experience slider sync + resume-based pre-fill.
// The page sets window.RESUME_YEARS = {resumeId: years} before loading this.
(function () {
  const RESUME_YEARS = window.RESUME_YEARS || {};

  document.querySelectorAll('[data-exp]').forEach(row => {
    const slider = row.querySelector('[data-exp-slider]');
    const num = row.querySelector('[data-exp-num]');
    const label = row.querySelector('[data-exp-level]');
    const levelFor = y => {
      if (y <= 1) return 'Entry level';
      if (y <= 4) return 'Junior / early career';
      if (y <= 9) return 'Intermediate / senior';
      if (y <= 14) return 'Senior / lead';
      if (y <= 24) return 'Management / principal';
      return 'Executive / senior leadership';
    };
    const paint = () => {
      if (num.value === '') { label.textContent = 'no experience filter'; return; }
      const y = parseInt(num.value, 10) || 0;
      label.textContent = (y >= 40 ? '40+ yrs — ' : '') + levelFor(y);
    };
    slider.addEventListener('input', () => { num.value = slider.value; paint(); });
    num.addEventListener('input', () => {
      if (num.value !== '') {
        const y = Math.max(0, Math.min(60, parseInt(num.value, 10) || 0));
        slider.value = Math.min(y, 40);
      }
      paint();
    });
    paint();
  });

  // Pre-fill the experience control from the selected resume's estimate.
  // On the edit page the box already has a saved value, so only auto-fill
  // when it starts empty and the user hasn't typed their own number.
  const resumeSel = document.getElementById('resume_id');
  const expNum = document.getElementById('alert-exp-years');
  if (resumeSel && expNum) {
    let userEdited = expNum.value !== '';
    expNum.addEventListener('input', () => { userEdited = true; });
    const fill = () => {
      if (userEdited) return;
      const est = RESUME_YEARS[resumeSel.value];
      expNum.value = (est === undefined || est === null) ? '' : est;
      expNum.dispatchEvent(new Event('input'));
      userEdited = false;  // programmatic input above shouldn't count
    };
    resumeSel.addEventListener('change', () => { userEdited = false; fill(); });
    if (expNum.value === '') fill();
  }
})();
