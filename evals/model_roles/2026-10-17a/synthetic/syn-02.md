What we learned red-teaming Corvalis-2 for six weeks

Tessellate AI asked us to stress-test Corvalis-2, their customer-support assistant model, before its public launch. This post summarises the campaign.

We ran 2,400 adversarial conversations against Corvalis-2 across five harm categories. Most attacks were written by our team, but a third were generated automatically: we used Brambling-7B, a small open model we fine-tuned on a corpus of past jailbreak transcripts, as an attacker that proposes follow-up turns when Corvalis-2 refuses. Brambling-7B was good at finding role-play framings, and it found 31 of the 58 successful attacks.

Corvalis-2 held up well on direct requests: it refused 99.1% of single-turn harmful prompts. It was weaker in long conversations. After twelve or more turns of friendly small talk, its refusal rate on the same prompts fell to 81%. Tessellate AI has since added a conversation-length-aware safety check, and in a retest of the 58 successful attacks Corvalis-2 refused 55.

We think the main lesson is that single-turn refusal numbers say little about how a deployed assistant behaves in long sessions.
