document.querySelectorAll('[data-filter-picker]').forEach(picker => {
    const panels = Array.from(picker.querySelectorAll('[data-filter-panel]'));
    const chooser = picker.querySelector('[data-filter-choice]');
    const active = new Set(panels.filter(panel => !panel.hidden).map(panel => panel.dataset.filterPanel));
    function sync() {
        const enabled = picker.dataset.enabled !== 'false';
        panels.forEach(panel => {
            const selected = active.has(panel.dataset.filterPanel);
            panel.hidden = !selected;
            panel.querySelectorAll('input, select, textarea').forEach(input => { input.disabled = !selected || !enabled; });
        });
        Array.from(chooser.options).forEach(option => { option.disabled = active.has(option.value); });
    }
    picker.querySelector('[data-add-filter]').addEventListener('click', () => {
        if (!chooser.value) return;
        active.add(chooser.value);
        chooser.value = '';
        sync();
    });
    panels.forEach(panel => panel.querySelector('[data-remove-filter]').addEventListener('click', () => {
        active.delete(panel.dataset.filterPanel);
        panel.querySelectorAll('input[type="checkbox"]').forEach(input => { input.checked = false; });
        sync();
    }));
    picker.querySelector('[data-clear-filters]').addEventListener('click', () => {
        active.clear();
        panels.forEach(panel => panel.querySelectorAll('input[type="checkbox"]').forEach(input => { input.checked = false; }));
        sync();
    });
    picker.addEventListener('filter-mode-change', sync);
    sync();
});
document.querySelectorAll('[data-assignment-form]').forEach(form => {
    function syncMode() {
        const individual = form.querySelector('[name="mode"]:checked')?.value === 'individual';
        const individualPanel = form.querySelector('[data-individual-mode]');
        const filterPanel = form.querySelector('[data-filter-mode]');
        individualPanel.hidden = !individual;
        individualPanel.querySelectorAll('input').forEach(input => { input.disabled = !individual; });
        filterPanel.hidden = individual;
        const picker = filterPanel.querySelector('[data-filter-picker]');
        picker.dataset.enabled = String(!individual);
        picker.dispatchEvent(new Event('filter-mode-change'));
    }
    form.querySelectorAll('[name="mode"]').forEach(input => input.addEventListener('change', syncMode));
    syncMode();
});
document.querySelectorAll('[data-question-editor]').forEach(form => {
    const textarea = form.querySelector('[name="options_text"]');
    const kind = form.querySelector('[name="answer_type"]');
    if (!textarea || !kind) return;
    const editor = document.createElement('div');
    const rows = document.createElement('div');
    const add = document.createElement('button');
    add.type = 'button'; add.textContent = 'Добавить вариант ответа';
    function addRow(value = '') {
        const row = document.createElement('div'); row.className = 'option-row';
        const input = document.createElement('input'); input.type = 'text'; input.maxLength = 200;
        input.value = value; input.setAttribute('aria-label', 'Вариант ответа');
        const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Убрать вариант';
        remove.addEventListener('click', () => { row.remove(); form.dispatchEvent(new Event('input', {bubbles:true})); }); row.append(input, remove); rows.append(row);
    }
    const initial = textarea.value.split(/\r?\n/).filter(value => value.trim());
    (initial.length ? initial : ['', '']).forEach(addRow);
    add.addEventListener('click', () => { if (rows.children.length < 50) addRow(); });
    editor.append(rows, add); textarea.after(editor); textarea.hidden = true;
    const fieldContainer = textarea.closest('p') || editor;
    const help = fieldContainer.querySelector('.helptext');
    if (help) help.textContent = 'Добавьте от 2 до 50 вариантов. Каждый вариант можно изменить или убрать.';
    function syncKind() {
        const choice = ['single_choice', 'multiple_choice'].includes(kind.value);
        editor.hidden = !choice;
        fieldContainer.hidden = !choice;
    }
    kind.addEventListener('change', syncKind); syncKind();
    form.addEventListener('submit', () => { textarea.value = Array.from(rows.querySelectorAll('input')).map(input => input.value).join('\n'); });
});
document.querySelectorAll('[data-section-image-editor]').forEach(form => {
    const remove = form.querySelector('[data-remove-image]');
    if (!remove) return;
    const undo = form.querySelector('[data-undo-image-removal]');
    const preview = form.querySelector('[data-image-preview]');
    const image = form.querySelector('[data-image-preview-content]');
    const savedImage = image.getAttribute('src') || '';
    const status = form.querySelector('[data-image-removal-status]');
    const value = form.querySelector('[name="remove_image"]');
    const upload = form.querySelector('[name="image"]');
    let pending = ['true', '1', 'on'].includes(value.value.toLowerCase());
    let selectedImage = null;
    function sync() {
        if (selectedImage) URL.revokeObjectURL(selectedImage);
        selectedImage = upload.files.length ? URL.createObjectURL(upload.files[0]) : null;
        const source = selectedImage || (!pending ? savedImage : '');
        if (source) image.src = source;
        else image.removeAttribute('src');
        value.value = pending ? 'true' : '';
        preview.hidden = !source;
        remove.hidden = !source;
        undo.hidden = !pending || !savedImage;
        status.hidden = !pending;
    }
    remove.addEventListener('click', event => {
        event.preventDefault();
        upload.value = '';
        pending = Boolean(savedImage);
        sync();
        value.dispatchEvent(new Event('change', {bubbles: true}));
        upload.focus();
    });
    undo.addEventListener('click', () => {
        pending = false;
        sync();
        value.dispatchEvent(new Event('change', {bubbles: true}));
        remove.focus();
    });
    upload.addEventListener('change', () => {
        if (upload.files.length) pending = false;
        sync();
    });
    sync();
});
// Не теряем набранный вопрос при переходе к следующему блоку.
const unsavedForms = new Set();
document.querySelectorAll('form[data-confirm-leave]').forEach(form => {
    if (form.querySelector('.errorlist')) unsavedForms.add(form);
    form.addEventListener('input', () => unsavedForms.add(form));
    form.addEventListener('change', () => unsavedForms.add(form));
    form.addEventListener('submit', () => unsavedForms.delete(form));
    form.addEventListener('reset', () => unsavedForms.delete(form));
});
window.addEventListener('beforeunload', event => {
    if (unsavedForms.size) { event.preventDefault(); event.returnValue = ''; }
});
