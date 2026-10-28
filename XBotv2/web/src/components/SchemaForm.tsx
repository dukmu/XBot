import { useEffect, useRef, useState } from "react";
import type { JsonObject } from "../api/types";
import {
  type FieldKind,
  type JsonSchema,
  type ResolvedField,
  UNSET_OPTION,
  defaultValue,
  fieldError,
  isRecord,
  optionKey,
  resolveField,
  resolveFields,
} from "./schemaForm";

export interface SchemaFormProps {
  schema: JsonSchema;
  value: JsonObject;
  onChange: (value: JsonObject) => void;
  path?: string;
  /** The document that owns `$defs`; nested forms inherit it. */
  root?: JsonSchema;
}

/** Render one plugin configuration from its declared JSON Schema. */
export function SchemaForm({ schema, value, onChange, path = "", root }: SchemaFormProps) {
  const document = root ?? schema;
  const fields = resolveFields(schema, document);
  if (!fields.length) {
    return <p className="schema-form-empty">This configuration declares no editable fields.</p>;
  }
  return (
    <div className="schema-fields" aria-label={path ? `${path} fields` : "Configuration fields"}>
      {fields.map((field) => (
        <FieldControl
          key={field.name}
          field={field}
          root={document}
          value={value[field.name]}
          onChange={(next) => onChange({ ...value, [field.name]: next })}
          onClear={() => {
            const cleared = { ...value };
            delete cleared[field.name];
            onChange(cleared);
          }}
        />
      ))}
    </div>
  );
}

/** Kinds whose control is a container: they take a whole row of the grid. */
const WIDE_KINDS = new Set<FieldKind>(["text", "array", "object", "mapping", "json"]);

function FieldControl({ field, root, value, onChange, onClear }: {
  field: ResolvedField;
  root: JsonSchema;
  value: unknown;
  onChange: (value: unknown) => void;
  onClear: () => void;
}) {
  const error = fieldError(field, value);
  const shell = (control: React.ReactNode) => (
    <FieldShell field={field} value={value} error={error} onChange={onChange} onClear={onClear}>
      {control}
    </FieldShell>
  );

  switch (field.kind) {
    case "boolean":
      return (
        <div className={`schema-field schema-field-boolean${error ? " invalid" : ""}`}>
          <label className="schema-field-boolean-box">
            <input
              type="checkbox"
              aria-label={field.label}
              checked={value === true}
              onChange={(event) => onChange(event.target.checked)}
            />
          </label>
          <FieldHeading field={field} value={value} onClear={onClear} />
          {field.description && <small className="schema-field-help">{field.description}</small>}
          {error && <small className="schema-field-error" role="alert">{error}</small>}
        </div>
      );
    case "enum": {
      const selected = field.options.find((option) => option.value === value);
      return shell(
        <select
          aria-label={field.label}
          value={selected ? optionKey(selected.value) : UNSET_OPTION}
          onChange={(event) => {
            const raw = event.target.value;
            if (raw === UNSET_OPTION) {
              onChange(null);
              return;
            }
            onChange(field.options.find((option) => optionKey(option.value) === raw)?.value ?? null);
          }}
        >
          {field.nullable && <option value={UNSET_OPTION}>— unset —</option>}
          {field.options.map((option, index) => (
            <option key={`${option.label}-${index}`} value={optionKey(option.value)}>{option.label}</option>
          ))}
        </select>,
      );
    }
    case "const":
      return shell(
        <input
          type="text"
          readOnly
          aria-label={field.label}
          value={field.options[0]?.label ?? String(value ?? "")}
        />,
      );
    case "integer":
    case "number":
      return shell(
        <input
          type="number"
          aria-label={field.label}
          value={typeof value === "number" ? String(value) : ""}
          min={field.schema.minimum ?? field.schema.exclusiveMinimum}
          max={field.schema.maximum ?? field.schema.exclusiveMaximum}
          step={field.kind === "integer" ? 1 : "any"}
          placeholder={typeof field.default === "number" ? String(field.default) : undefined}
          onChange={(event) => {
            const raw = event.target.value;
            onChange(raw === "" ? null : field.kind === "integer" ? Math.trunc(Number(raw)) : Number(raw));
          }}
        />,
      );
    case "text":
      return shell(
        <textarea
          aria-label={field.label}
          value={typeof value === "string" ? value : ""}
          maxLength={field.schema.maxLength}
          onChange={(event) => onChange(event.target.value)}
        />,
      );
    case "string":
      return shell(
        <input
          type="text"
          aria-label={field.label}
          value={typeof value === "string" ? value : ""}
          minLength={field.schema.minLength}
          maxLength={field.schema.maxLength}
          placeholder={typeof field.default === "string" ? field.default : undefined}
          onChange={(event) => onChange(event.target.value)}
        />,
      );
    case "array":
      return shell(<ArrayControl field={field} root={root} value={value} onChange={onChange} />);
    case "object":
      return (
        <fieldset className={`schema-field schema-field-object${error ? " invalid" : ""}`}>
          <legend>
            <FieldHeading field={field} value={value} onClear={onClear} />
          </legend>
          {field.description && <small className="schema-field-help">{field.description}</small>}
          <SchemaForm
            path={field.name}
            schema={field.schema}
            root={root}
            value={isRecord(value) ? value : {}}
            onChange={onChange}
          />
          {error && <small className="schema-field-error" role="alert">{error}</small>}
        </fieldset>
      );
    case "mapping":
      return shell(<MappingControl field={field} root={root} value={value} onChange={onChange} />);
    default:
      return shell(
        <JsonControl
          label={field.label}
          description={field.description}
          value={value}
          onChange={onChange}
        />,
      );
  }
}

/** How the schema's own default reads next to a field the layer does not set. */
function defaultHint(value: unknown): string {
  if (typeof value === "string") {
    if (!value) return "default empty";
    return `default ${value.length > 28 ? `${value.slice(0, 28)}…` : value}`;
  }
  return `default ${JSON.stringify(value)}`;
}

/** Label row shared by every control, including the `Clear` layer action. */
function FieldHeading({ field, value, onClear }: {
  field: ResolvedField;
  value: unknown;
  onClear: () => void;
}) {
  const hint = [
    field.hint,
    value === undefined && field.default !== undefined && field.default !== null
      ? defaultHint(field.default)
      : "",
  ].filter(Boolean).join(" · ");
  return (
    <span className="schema-field-label">
      <span className="schema-field-name">{field.label}{field.required && <em aria-hidden="true"> *</em>}</span>
      {hint && <span className="schema-field-hint">{hint}</span>}
      {value !== undefined && (
        <button
          type="button"
          className="schema-field-clear"
          title="Remove this layer's value"
          onClick={onClear}
        >
          Clear
        </button>
      )}
    </span>
  );
}

function FieldShell({ field, value, error, onChange, onClear, children }: {
  field: ResolvedField;
  value: unknown;
  error: string;
  onChange: (value: unknown) => void;
  onClear: () => void;
  children: React.ReactNode;
}) {
  const classes = [
    "schema-field",
    WIDE_KINDS.has(field.kind) ? "schema-field-wide" : "",
    error ? "invalid" : "",
  ].filter(Boolean).join(" ");
  return (
    <div className={classes}>
      <FieldHeading field={field} value={value} onClear={onClear} />
      {children}
      {field.description && <small className="schema-field-help">{field.description}</small>}
      {error && <small className="schema-field-error" role="alert">{error}</small>}
    </div>
  );
}

function ArrayControl({ field, root, value, onChange }: {
  field: ResolvedField;
  root: JsonSchema;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const items = Array.isArray(value) ? value : [];
  const itemSchema = field.item ?? {};
  const itemFields = itemSchema.properties ? resolveFields(itemSchema, root) : [];
  const replace = (index: number, next: unknown) => {
    const copy = [...items];
    copy[index] = next;
    onChange(copy);
  };
  const add = () => {
    if (itemFields.length) {
      onChange([...items, Object.fromEntries(itemFields.map((entry) => [entry.name, defaultValue(entry)]))]);
      return;
    }
    onChange([...items, defaultValue(resolveField(field.name, field.item ?? {}, root, false))]);
  };
  return (
    <div className="schema-array">
      {items.map((item, index) => (
        <div className={`schema-array-item${itemFields.length ? "" : " scalar"}`} key={index}>
          {itemFields.length ? (
            <fieldset className="schema-array-card">
              <legend>{field.label} {index + 1}</legend>
              <SchemaForm
                path={`${field.name}[${index}]`}
                schema={itemSchema}
                root={root}
                value={isRecord(item) ? item : {}}
                onChange={(next) => replace(index, next)}
              />
            </fieldset>
          ) : (
            <ScalarItemControl
              label={`${field.label} ${index + 1}`}
              schema={itemSchema}
              value={item}
              onChange={(next) => replace(index, next)}
            />
          )}
          <button
            type="button"
            className="schema-array-remove"
            aria-label={`Remove ${field.label} ${index + 1}`}
            onClick={() => onChange(items.filter((_, position) => position !== index))}
          >
            Remove
          </button>
        </div>
      ))}
      <button type="button" className="schema-array-add" onClick={add}>
        Add {field.label}
      </button>
    </div>
  );
}

function ScalarItemControl({ label, schema, value, onChange }: {
  label: string;
  schema: JsonSchema;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const options = schema.enum ?? (schema.const !== undefined ? [schema.const] : []);
  if (options.length) {
    return (
      <select
        aria-label={label}
        value={optionKey(options.find((option) => option === value) ?? options[0])}
        onChange={(event) => {
          const raw = event.target.value;
          const next = options.find((option) => optionKey(option) === raw);
          if (next !== undefined) onChange(next);
        }}
      >
        {options.map((option, index) => (
          <option key={`${String(option)}-${index}`} value={optionKey(option)}>{String(option)}</option>
        ))}
      </select>
    );
  }
  if (schema.type === "boolean") {
    return (
      <label className="schema-array-boolean">
        <input
          type="checkbox"
          aria-label={label}
          checked={value === true}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span>{label}</span>
      </label>
    );
  }
  if (schema.type === "integer" || schema.type === "number") {
    return (
      <input
        type="number"
        aria-label={label}
        value={typeof value === "number" ? String(value) : ""}
        min={schema.minimum}
        max={schema.maximum}
        step={schema.type === "integer" ? 1 : "any"}
        onChange={(event) => onChange(event.target.value === "" ? null : Number(event.target.value))}
      />
    );
  }
  if (schema.type === "object" || schema.properties) {
    return <JsonControl label={`${label} JSON`} description="" value={value} onChange={onChange} />;
  }
  return (
    <input
      type="text"
      aria-label={label}
      value={typeof value === "string" ? value : ""}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

function MappingControl({ field, root, value, onChange }: {
  field: ResolvedField;
  root: JsonSchema;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const entries = isRecord(value) ? Object.entries(value) : [];
  const valueSchema = field.valueSchema;
  const valueFields = valueSchema?.properties ? resolveFields(valueSchema, root) : [];
  const stringValues = valueSchema?.type === "string" || Boolean(valueSchema?.enum);
  const replace = (previous: string, key: string, next: unknown) => {
    const copy: JsonObject = {};
    for (const [entryKey, entryValue] of entries) {
      copy[entryKey === previous ? key : entryKey] = entryKey === previous ? next : entryValue;
    }
    onChange(copy);
  };
  const blank = () => {
    if (valueFields.length) {
      return Object.fromEntries(valueFields.map((entry) => [entry.name, defaultValue(entry)]));
    }
    return stringValues ? "" : null;
  };
  return (
    <div className="schema-mapping">
      {entries.map(([key, entryValue]) => (
        <div className={`schema-mapping-row${valueFields.length ? " has-object" : ""}`} key={key}>
          <input
            type="text"
            className="schema-mapping-name"
            aria-label={`${field.label} name`}
            defaultValue={key}
            placeholder="name"
            onBlur={(event) => {
              const next = event.target.value.trim();
              if (next && next !== key) replace(key, next, entryValue);
            }}
          />
          {valueFields.length ? (
            <SchemaForm
              path={`${field.label} ${key}`}
              schema={valueSchema as JsonSchema}
              root={root}
              value={isRecord(entryValue) ? entryValue : {}}
              onChange={(next) => replace(key, key, next)}
            />
          ) : stringValues ? (
            <input
              type="text"
              aria-label={`${field.label} ${key}`}
              value={typeof entryValue === "string" ? entryValue : ""}
              onChange={(event) => replace(key, key, event.target.value)}
            />
          ) : (
            <JsonControl
              label={`${field.label} ${key}`}
              description=""
              value={entryValue}
              onChange={(next) => replace(key, key, next)}
            />
          )}
          <button
            type="button"
            className="schema-mapping-remove"
            aria-label={`Remove ${field.label} ${key}`}
            onClick={() => {
              const copy: JsonObject = { ...(isRecord(value) ? value : {}) };
              delete copy[key];
              onChange(copy);
            }}
          >
            Remove
          </button>
        </div>
      ))}
      <button
        type="button"
        className="schema-mapping-add"
        onClick={() => onChange({ ...(isRecord(value) ? value : {}), "": blank() })}
      >
        Add {field.label}
      </button>
    </div>
  );
}

function JsonControl({ label, description, value, onChange }: {
  label: string;
  description: string;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const serialized = JSON.stringify(value ?? null, null, 2);
  const [draft, setDraft] = useState(serialized);
  const [error, setError] = useState("");
  const echoed = useRef(serialized);
  useEffect(() => {
    // Only re-render the text when the value changed elsewhere: reformatting
    // our own edit would move the caret on every keystroke.
    if (serialized === echoed.current) return;
    echoed.current = serialized;
    setDraft(serialized);
    setError("");
  }, [serialized]);
  return (
    <div className="schema-json">
      <textarea
        aria-label={`${label} JSON`}
        value={draft}
        spellCheck={false}
        onChange={(event) => {
          const next = event.target.value;
          setDraft(next);
          try {
            const parsed: unknown = JSON.parse(next);
            echoed.current = JSON.stringify(parsed ?? null, null, 2);
            setError("");
            onChange(parsed);
          } catch {
            setError("Enter valid JSON to apply this field.");
          }
        }}
      />
      {description && <small className="schema-field-help">{description}</small>}
      {error && <small className="schema-field-error" role="alert">{error}</small>}
    </div>
  );
}
