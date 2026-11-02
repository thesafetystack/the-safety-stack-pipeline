rget models in a family that share a tokenizer and a training corpus, so that differences between them can be attributed to scale rather than to data, and for which intermediate checkpoints are available at regular intervals. 

4 

Preprint 



<!-- Start of picture text -->
(a) Loss (b) Exact match (c) Paraphrase rate<br>4.5 4.0 3.5 3.0 2.5<br>Split<br>held-outeasy held-outhard memorised paraphrased<br>60 45 30 15 0.75 0.60 0.45 0.30 0.15<br>25k 50k 75k 100k 25k 50k 75k 100k 25k 50k 75k 100k<br>Training step Training step Training step<br>medium (small, large: Appendix C)<br>%%% 0.00.10.20.3 1520253035
<!-- End of picture text -->

Figure 2: **Training dynamics of the medium model.** Loss on the three evaluation splits, the share of prompts answered by exact recall of a training sequence, and the share answered by a paraphrase of one, over the first 100k steps. Exact recall rises fastest in the first few thousand steps; paraphrase rises later and more slowly. The small and large models are shown in Appendix C. 

Each figure in the main text has a counterpart in the appendix with the same quantities for the other model sizes. We track every sampled sequence across checkpoints and classify each prompt at each checkpoint as answered from recall, answered by paraphrase, or not answered from training data at all. 

The classification depends on a threshold for how close an answer must be to a training sequence to count as recall. We set it on a small hand-labelled sample and report results at two other thresholds in Appendix D; the ordering of the curves in Figure 2 is the same at all three. 

A second surprise was that a share of the held-out hard prompts, between one in ten and one in five depending on the model size, is answered by paraphrase at some checkpoint and then no longer at later ones. We return to this forgetting pattern in Section 5. 
