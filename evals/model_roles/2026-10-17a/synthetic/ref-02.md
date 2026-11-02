An automated grader for long agent transcripts

Grading long agent transcripts by hand does not scale, so for this study we used Claude, in its 3.5 Sonnet version, as the grader. The grader read each transcript together with a rubric of eight failure types and returned one label per failure type.

To check the grader, two of us labelled a random sample of 300 transcripts independently. The grader agreed with the human majority label on 91% of the sample, which is close to the agreement between the two human labellers (93%). Most disagreements were on the "silent scope change" failure type, where the humans also disagreed most.

All results in the rest of the paper use the grader's labels. We release the rubric and the prompt we gave the grader.
