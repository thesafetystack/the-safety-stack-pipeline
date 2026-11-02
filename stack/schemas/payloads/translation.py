"""``TRANSLATION`` payload (D755): machine translations of a non-English document's short fields.

The original ``title`` and ``abstract`` stay in ``metadata`` as the sources gave them; this payload adds English
versions beside them: ``from_lang`` (ISO 639-1 of the original, as the model detected it), ``title_en``,
``abstract_en`` (NULL when the document has none, or when ``from_lang`` is ``en``: then nothing is translated and
the record is ``empty``) and ``method`` (``llm``: a machine translation; the record's metadata names the model and
prompt). A ``fields`` override of this payload (target ``translation``) may supply a human translation with
``method: human``.
"""

from __future__ import annotations

from stack.schemas.structs import ST, S

TRANSLATION = ST(from_lang=S, title_en=S, abstract_en=S, method=S)
