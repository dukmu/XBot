/**
 * Register dsh's markdown grammar extensions on the parser react-markdown builds.
 *
 * XBot renders markdown through react-markdown, whose `remark-parse` reads its
 * micromark syntax extensions from the processor's data bag; dsh's own pipeline
 * hands them straight to `fromMarkdown`. Registering them here is therefore the
 * only adaptation the ports need: the grammar, and so the parsed tree, is
 * upstream's. The order matches dsh's `parseGfmWithMath`: GFM, then the
 * CJK-friendly attention construct, then the math compatibility delimiters,
 * with `remark-math` supplying dollar math last.
 */

import type { Plugin } from "unified";
import { cjkFriendlyStrong } from "./cjkFriendlyStrong";
import { mathCompatibility } from "./mathCompatibility";

type MicromarkData = { micromarkExtensions?: unknown[] };

export const remarkDshGrammar: Plugin = function remarkDshGrammar() {
  const data = this.data() as MicromarkData;
  const extensions = data.micromarkExtensions ?? (data.micromarkExtensions = []);
  extensions.push(cjkFriendlyStrong(), mathCompatibility());
};
