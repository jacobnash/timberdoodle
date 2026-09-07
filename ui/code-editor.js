// CodeMirror 6 wrapper, loaded from jsdelivr's dependency-resolving +esm
// endpoint - no bundler of our own, same buildless spirit as the rest of
// ui/. Declarative: any <textarea data-editor-lang="json|python"> gets
// swapped for a real editor on load. derivations.js/alarms.js don't know
// this file exists - they keep reading/writing textarea.value and
// listening for "input" exactly like before.
// Pinned via esm.sh's ?deps= so codemirror/lang-json/lang-python resolve
// the *same* @codemirror/state,view,language,autocomplete,@lezer/common
// builds - without this they each drag in their own copy and CodeMirror
// throws "multiple instances of @codemirror/state" at construction time.
const DEPS = "@codemirror/state@6.7.4,@codemirror/view@6.43.11,@codemirror/language@6.12.4,@codemirror/autocomplete@6.20.3,@lezer/common@1.5.2";
const [{ EditorView, basicSetup }, { json }, { python }] = await Promise.all([
  import(`https://esm.sh/codemirror@6.0.2?deps=${DEPS}`),
  import(`https://esm.sh/@codemirror/lang-json@6.0.2?deps=${DEPS}`),
  import(`https://esm.sh/@codemirror/lang-python@6.2.1?deps=${DEPS}`),
]);

const LANGS = { json, python };

function attach(textarea) {
  const lang = LANGS[textarea.dataset.editorLang];
  const view = new EditorView({
    doc: textarea.value,
    extensions: [
      basicSetup,
      ...(lang ? [lang()] : []),
      EditorView.updateListener.of((update) => {
        if (update.docChanged) textarea.dispatchEvent(new Event("input"));
      }),
    ],
  });

  // The rest of the app only ever touches textarea.value - overriding the
  // accessor (instead of rewriting every caller to a CodeMirror API) is
  // the entire integration.
  Object.defineProperty(textarea, "value", {
    get: () => view.state.doc.toString(),
    set: (v) => view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: v ?? "" } }),
  });

  // validateJsonField() toggles .invalid on the (now hidden) textarea -
  // mirror it onto the visible editor.
  const mirrorInvalid = () => view.dom.classList.toggle("invalid", textarea.classList.contains("invalid"));
  new MutationObserver(mirrorInvalid).observe(textarea, { attributes: true, attributeFilter: ["class"] });
  mirrorInvalid();

  // Lets callers jump the user to a 1-based line number an error refers to
  // (a sandbox.py SandboxError, a SyntaxError, a JSON.parse position) -
  // selecting the line is enough to make it visually distinct, no
  // Decoration extension needed.
  textarea.markErrorLine = (line) => {
    const clamped = Math.min(Math.max(1, line), view.state.doc.lines);
    const { from, to } = view.state.doc.line(clamped);
    view.dispatch({ selection: { anchor: from, head: to }, scrollIntoView: true });
    view.focus();
  };

  view.dom.style.height = `${(textarea.rows || 8) * 1.4}em`;
  textarea.style.display = "none";
  textarea.after(view.dom);
}

document.querySelectorAll("textarea[data-editor-lang]").forEach(attach);
