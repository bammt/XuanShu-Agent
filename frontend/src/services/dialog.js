import { reactive } from "vue";

export const dialogState = reactive({
  open: false,
  kind: "confirm",
  title: "",
  message: "",
  confirmLabel: "确认",
  cancelLabel: "取消",
  danger: false,
  inputLabel: "",
  inputType: "text",
  inputMinLength: 0,
  inputValue: "",
  placeholder: "",
});

let resolver = null;

function openDialog(options) {
  if (resolver) resolver(options.kind === "prompt" ? null : false);
  Object.assign(dialogState, {
    open: true,
    kind: options.kind || "confirm",
    title: options.title || "请确认",
    message: options.message || "",
    confirmLabel: options.confirmLabel || "确认",
    cancelLabel: options.cancelLabel || "取消",
    danger: Boolean(options.danger),
    inputLabel: options.inputLabel || "",
    inputType: options.inputType || "text",
    inputMinLength: Number(options.inputMinLength || 0),
    inputValue: options.inputValue || "",
    placeholder: options.placeholder || "",
  });
  return new Promise((resolve) => {
    resolver = resolve;
  });
}

export function confirmDialog(options = {}) {
  return openDialog({ ...options, kind: "confirm" });
}

export function promptDialog(options = {}) {
  return openDialog({ ...options, kind: "prompt" });
}

export function closeDialog(confirmed) {
  if (!dialogState.open) return;
  const resolve = resolver;
  resolver = null;
  dialogState.open = false;
  if (!resolve) return;
  resolve(
    confirmed
      ? dialogState.kind === "prompt"
        ? dialogState.inputValue
        : true
      : dialogState.kind === "prompt"
        ? null
        : false,
  );
}
