document.querySelectorAll('form').forEach(form => {
    const rows = Array.from(form.querySelectorAll('[data-conditional-question]'));
    const navigationElement = document.getElementById(form.dataset.surveyNavigation || '');
    const navigation = navigationElement ? JSON.parse(navigationElement.textContent) : null;
    if (!rows.length && !navigation) return;
    const byId = new Map(rows.map(row => [row.dataset.conditionalQuestion, row]));
    function sync() {
        const states = new Map();
        function visible(id, visiting = new Set()) {
            if (states.has(id)) return states.get(id);
            const row = byId.get(id);
            if (!row || visiting.has(id)) return false;
            const parent = row.dataset.parentQuestion;
            if (!parent) {
                states.set(id, true);
                return true;
            }
            const branch = new Set(visiting);
            branch.add(id);
            const active = visible(parent, branch) && Array.from(byId.get(parent).querySelectorAll('input:checked'))
                .some(input => input.value === row.dataset.parentOption);
            states.set(id, active);
            return active;
        }
        rows.forEach(row => visible(row.dataset.conditionalQuestion));
        rows.forEach(row => {
            const active = states.get(row.dataset.conditionalQuestion);
            row.hidden = !active;
            row.querySelectorAll('input, textarea, select').forEach(input => {
                input.disabled = !active;
                if (!active) {
                    if (input.type === 'radio' || input.type === 'checkbox') input.checked = false;
                    else input.value = '';
                }
            });
        });
        if (navigation) {
            const answers = {...navigation.answers};
            rows.forEach(row => {
                const inputs = Array.from(row.querySelectorAll('input, textarea, select'));
                answers[`q_${row.dataset.conditionalQuestion}`] = inputs.filter(input =>
                    !input.disabled && (input.type !== 'checkbox' && input.type !== 'radio' || input.checked)
                ).map(input => input.value);
            });
            const blocks = new Map(navigation.blocks.map(block => [String(block.id), block]));
            const questions = new Map(navigation.questions.map(question => [String(question.id), question]));
            const active = new Map();
            function matches(kind, id, visiting = new Set()) {
                const key = `${kind}:${id}`;
                if (active.has(key)) return active.get(key);
                const node = (kind === 'block' ? blocks : questions).get(String(id));
                if (!node || visiting.has(key)) return false;
                const branch = new Set(visiting);
                branch.add(key);
                let shown = kind === 'block' || matches('block', node.block, branch);
                if (shown && node.option) {
                    const raw = answers[`q_${node.question}`];
                    const selected = Array.isArray(raw) ? raw : [raw];
                    shown = matches('question', node.question, branch) && selected.map(String).includes(String(node.option));
                }
                active.set(key, shown);
                return shown;
            }
            const current = navigation.blocks.findIndex(block => block.id === navigation.current_block);
            const next = navigation.blocks.slice(current + 1).some(block => matches('block', block.id));
            form.querySelector('[data-next-block]').hidden = !next;
            form.querySelector('[data-submit-survey]').hidden = next;
        }
    }
    form.addEventListener('change', sync);
    sync();
});
