/**
 * Pydantic/JSON Schema projection for the plugin configuration editor.
 *
 * Pydantic v2 emits optional fields as ``anyOf: [<type>, {type: "null"}]`` and
 * hoists nested models and enums into ``$defs`` behind ``$ref``.  Resolving
 * those constructs here keeps the renderer small and makes every field kind
 * testable without a DOM.
 */

import type { JsonObject } from "../api/types";

export interface JsonSchema {
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  definitions?: Record<string, JsonSchema>;
  type?: string | string[];
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  const?: unknown;
  anyOf?: JsonSchema[];
  oneOf?: JsonSchema[];
  allOf?: JsonSchema[];
  properties?: Record<string, JsonSchema>;
  required?: string[];
  additionalProperties?: boolean | JsonSchema;
  items?: JsonSchema;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  minLength?: number;
  maxLength?: number;
  pattern?: string;
  format?: string;
}

export type FieldKind =
  | "boolean"
  | "enum"
  | "const"
  | "integer"
  | "number"
  | "string"
  | "text"
  | "array"
  | "object"
  | "mapping"
  | "json";

export interface EnumOption {
  label: string;
  value: unknown;
}

export interface ResolvedField {
  name: string;
  schema: JsonSchema;
  kind: FieldKind;
  nullable: boolean;
  required: boolean;
  label: string;
  description: string;
  hint: string;
  options: EnumOption[];
  item?: JsonSchema;
  valueSchema?: JsonSchema;
  default?: unknown;
}

const MAX_REF_DEPTH = 12;

function humanize(name: string): string {
  const spaced = name.replace(/_/g, " ").trim();
  return spaced ? spaced.charAt(0).toUpperCase() + spaced.slice(1) : name;
}

function descriptionOf(schema: JsonSchema): string {
  return typeof schema.description === "string" ? schema.description.trim() : "";
}

/** Drop JSON Schema's own metadata noise from a description-only field. */
export function resolveSchema(
  schema: JsonSchema | undefined,
  root?: JsonSchema,
  depth = 0,
): JsonSchema {
  if (!schema || depth > MAX_REF_DEPTH) return {};
  const definitions = root?.$defs ?? root?.definitions ?? {};
  if (schema.$ref) {
    const name = schema.$ref.split("/").pop() || "";
    const target = definitions[name];
    if (!target) return {};
    const { $ref: _ref, ...rest } = schema;
    return { ...resolveSchema(target, root, depth + 1), ...rest };
  }
  if (schema.allOf?.length) {
    return schema.allOf.reduce<JsonSchema>(
      (merged, part) => ({ ...merged, ...resolveSchema(part, root, depth + 1) }),
      { ...schema, allOf: undefined },
    );
  }
  return schema;
}

/** Split ``anyOf``/``oneOf`` into the renderable branch and its nullability. */
function selectBranch(schema: JsonSchema): { branch: JsonSchema; nullable: boolean } {
  const variants = schema.anyOf ?? schema.oneOf;
  if (!variants?.length) {
    const types = Array.isArray(schema.type) ? schema.type : [schema.type];
    return {
      branch: { ...schema, type: types.find((entry) => entry !== "null") },
      nullable: types.includes("null"),
    };
  }
  const nullable = variants.some(
    (variant) => variant.type === "null" || variant.const === null,
  );
  const branch = variants.find(
    (variant) => variant.type !== "null" && variant.const !== null,
  );
  if (!branch || variants.length - (nullable ? 1 : 0) > 1) {
    // A genuine union has no single control; keep it as a JSON field.
    return { branch: { ...schema, type: "object", properties: undefined }, nullable };
  }
  const { anyOf: _anyOf, oneOf: _oneOf, ...rest } = schema;
  return {
    branch: { ...rest, ...branch },
    nullable,
  };
}

function enumOptions(schema: JsonSchema): EnumOption[] {
  const values = schema.const !== undefined
    ? [schema.const]
    : schema.enum ?? [];
  return values.map((value) => ({
    label: typeof value === "string" ? value : JSON.stringify(value),
    value,
  }));
}

/**
 * The DOM value for an enum member. A string member keeps its own spelling so
 * the rendered `<select>` stays readable; anything else is tagged to stay
 * distinct from a string that happens to look the same.
 */
export function optionKey(value: unknown): string {
  return typeof value === "string" ? value : `#${JSON.stringify(value)}`;
}

/** `<select>` value used when an optional enum has no member selected. */
export const UNSET_OPTION = "\u0000unset";

function fieldKind(schema: JsonSchema): FieldKind {
  if (schema.const !== undefined) return "const";
  if (schema.enum?.length) return "enum";
  if (schema.type === "boolean") return "boolean";
  if (schema.type === "integer") return "integer";
  if (schema.type === "number") return "number";
  if (schema.type === "array" || schema.items) return "array";
  if (schema.type === "object" || schema.properties) {
    if (schema.properties) return "object";
    if (schema.additionalProperties && typeof schema.additionalProperties === "object") {
      return "mapping";
    }
    return "json";
  }
  if (schema.type === "string") {
    const long = (schema.maxLength ?? 0) > 160
      || schema.format === "multiline"
      || schema.format === "text";
    return long ? "text" : "string";
  }
  return "json";
}

function hintOf(field: JsonSchema, kind: FieldKind): string {
  const parts: string[] = [];
  if (kind === "integer" || kind === "number") {
    const { minimum, maximum, exclusiveMinimum, exclusiveMaximum } = field;
    if (minimum !== undefined && maximum !== undefined) parts.push(`${minimum}–${maximum}`);
    else if (minimum !== undefined) parts.push(`≥ ${minimum}`);
    else if (exclusiveMinimum !== undefined) parts.push(`> ${exclusiveMinimum}`);
    if (maximum !== undefined && minimum === undefined) parts.push(`≤ ${maximum}`);
    if (exclusiveMaximum !== undefined && maximum === undefined) parts.push(`< ${exclusiveMaximum}`);
    if (kind === "integer") parts.push("whole number");
  }
  if (kind === "string" || kind === "text") {
    if (field.minLength !== undefined) parts.push(`≥ ${field.minLength} chars`);
    if (field.maxLength !== undefined) parts.push(`≤ ${field.maxLength} chars`);
    if (field.pattern) parts.push(`matches ${field.pattern}`);
  }
  return parts.join(" · ");
}

export function resolveField(
  name: string,
  raw: JsonSchema,
  root: JsonSchema,
  required: boolean,
): ResolvedField {
  const resolved = resolveSchema(raw, root);
  const { branch, nullable } = selectBranch(resolved);
  const kind = fieldKind(branch);
  const field: ResolvedField = {
    name,
    schema: branch,
    kind,
    nullable,
    required,
    label: typeof branch.title === "string" && branch.title.trim()
      ? branch.title.trim()
      : humanize(name),
    description: descriptionOf(branch) || descriptionOf(resolved),
    hint: hintOf(branch, kind),
    options: enumOptions(branch),
    default: branch.default,
  };
  if (kind === "array" && branch.items) {
    field.item = resolveSchema(branch.items, root);
  }
  if (kind === "mapping" && typeof branch.additionalProperties === "object") {
    field.valueSchema = resolveSchema(branch.additionalProperties, root);
  }
  return field;
}

export function isRecord(value: unknown): value is JsonObject {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function resolveFields(
  schema: JsonSchema,
  root: JsonSchema,
): ResolvedField[] {
  const resolved = resolveSchema(schema, root);
  const required = new Set(resolved.required ?? []);
  return Object.entries(resolved.properties ?? {}).map(([name, property]) =>
    resolveField(name, property, root, required.has(name)));
}

/** The value a field starts from when it has never been set. */
export function defaultValue(field: ResolvedField): unknown {
  if (field.default !== undefined) return field.default;
  if (field.kind === "enum" && field.options.length) return field.options[0].value;
  switch (field.kind) {
    case "boolean":
      return false;
    case "integer":
    case "number":
      return field.nullable ? null : 0;
    case "array":
      return [];
    case "object":
      return {};
    case "mapping":
      return {};
    case "json":
      return field.nullable ? null : {};
    default:
      return field.nullable ? null : "";
  }
}

function isEmptyValue(field: ResolvedField, value: unknown): boolean {
  if (value === undefined || value === null) return true;
  if (field.kind === "string" || field.kind === "text") return String(value).trim() === "";
  if (field.kind === "array") return Array.isArray(value) && value.length === 0;
  if (field.kind === "mapping") return isRecord(value) && Object.keys(value).length === 0;
  return false;
}

/** One message per field, or an empty string when the value is acceptable. */
export function fieldError(field: ResolvedField, value: unknown): string {
  if (field.required && isEmptyValue(field, value)) {
    return `${field.label} is required.`;
  }
  if (value === undefined || value === null) return "";
  if (field.kind === "integer" && !Number.isInteger(value)) {
    return `${field.label} must be a whole number.`;
  }
  if (field.kind === "number" && typeof value !== "number") {
    return `${field.label} must be a number.`;
  }
  if (
    (field.kind === "number" || field.kind === "integer")
    && typeof value === "number"
  ) {
    const { minimum, maximum, exclusiveMinimum, exclusiveMaximum } = field.schema;
    if (minimum !== undefined && value < minimum) return `${field.label} must be ≥ ${minimum}.`;
    if (maximum !== undefined && value > maximum) return `${field.label} must be ≤ ${maximum}.`;
    if (exclusiveMinimum !== undefined && value <= exclusiveMinimum) {
      return `${field.label} must be > ${exclusiveMinimum}.`;
    }
    if (exclusiveMaximum !== undefined && value >= exclusiveMaximum) {
      return `${field.label} must be < ${exclusiveMaximum}.`;
    }
  }
  if ((field.kind === "string" || field.kind === "text") && typeof value === "string") {
    const { minLength, maxLength, pattern } = field.schema;
    if (minLength !== undefined && value.length < minLength) {
      return `${field.label} needs at least ${minLength} characters.`;
    }
    if (maxLength !== undefined && value.length > maxLength) {
      return `${field.label} allows at most ${maxLength} characters.`;
    }
    if (pattern) {
      try {
        if (!new RegExp(pattern).test(value)) {
          return `${field.label} must match ${pattern}.`;
        }
      } catch {
        // An unparsable pattern is reported by the schema owner, not the form.
      }
    }
  }
  if (field.kind === "enum" && field.options.length) {
    const allowed = field.options.some((option) => option.value === value);
    if (!allowed) return `${field.label} must be one of the declared options.`;
  }
  return "";
}

/** Every field error in a schema, keyed by the field's dotted path. */
export function schemaErrors(
  schema: JsonSchema,
  value: JsonObject,
  path = "",
  root: JsonSchema = schema,
): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const field of resolveFields(schema, root)) {
    const fieldPath = path ? `${path}.${field.name}` : field.name;
    const message = fieldError(field, value[field.name]);
    if (message) errors[fieldPath] = message;
    const nested = value[field.name];
    if (field.kind === "object" && isRecord(nested)) {
      Object.assign(errors, schemaErrors(field.schema, nested, fieldPath, root));
    }
    if (field.kind === "array" && field.item && Array.isArray(nested)) {
      nested.forEach((item, index) => {
        if (isRecord(item)) {
          Object.assign(errors, schemaErrors(field.item as JsonSchema, item, `${fieldPath}.${index}`, root));
        }
      });
    }
  }
  return errors;
}
