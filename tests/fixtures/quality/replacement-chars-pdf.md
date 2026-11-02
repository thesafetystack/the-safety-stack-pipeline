025; Kamath et al., 2023) probe spatial memory and relational language; and RAVEN (Zhang et al., 2019) isolates abstract visual analogy. HSS extends both directions to natural media and to inferences about what is _not_ observed: an alien viewpoint the camera never occupies, a rope whose tension is visible only in its sag, an event that the clip sets up but never shows. 

**Tool-augmented inspection.** ZeroBench and VisualToolBench (Roberts et al., 2025; Guo et al., 2025) show that letting a model crop, zoom and re-sample improves visual reasoning, and motivate our agentic condition. Section 3.3 uses that lever diagnostically rather than as a method contribution: if HSS failures were failures of looking, active inspection should repair them, and the extent to which it does not (Figure 4) is evidence about what the benchmark measures. 

**Automated grading and its validity.** Open-ended grading at this scale requires an LLM judge (Zheng et al., 2023), which imports known failure modes, self-preference (Panickssery et al., 2024) foremost among them. We audit rather than assume: three judges from three vendors, agreement and _κ_ per pair, and a self-grading contrast (Section 3.4, Appendix I). 

## B BENCHMARK DETAILS 

This appendix documents the released benchmark: what it contains (Appendix B.1), how each task was built and reviewed (Appendix B.2), and how the taxonomy was arrived at (Appendix B.3). 

### B.1 COMPOSITION AND STATISTICS 

Four properties of the composition in Table 3 matter for the analysis; Figure 7 shows the same corpus by scene type and clip length. First, no domain dominates the benchmark: the largest, _physical & spatial logic_ , holds 176 _/_ 522 tasks and the smallest, _abstract & contextual inference_ , holds 102, so every domain-level cell is large enough to carry a claim, and the smallest subdomain still holds 27. Second, the media split is near-even (288 images against 234 video clips, 17 _._ 6 hours 

14 



<!-- Start of picture text -->
��������������<br>�������������� ���,(���� �� & �( ���� ���� ������,���<br>���������������� ���'��  ���� ���)��&� ��)�� ����� �&( ���0������0������<br>�%2�& �(�� �� �� �� �� �������<br>������������������� �,��&�(<br>�������������� ��),���(�� �� �� �� �� ������<br>��0��(<br>������ ��� �� �� �� �� �� ������<br>�����1�0�(<br>����������� �� &�( �� �� �� �� ������<br>������������� ������������ ����������&��<br>������������ ������������� � �� �� �� ���<br>����� ������������������ ������������������ �%��������%����)�&����&)��������(b)<br>�� ��� ��������<br>��������������<br>�������������������<br>��<br>�<br>�� ��� ����<br>�������������� �������������� ����������������������������<br>����������������<br>(a) (c)<br>���������������������<br>������������������ ����������������<br>����� �����<br>�����<br>��������<br>��������<br>�����������<br>�������<br>����������������<br>�������������������<br>�����������<br>��������<br>��������<br>���������� ��������������<br>�������������� ���������������������<br>���������������� ���������������������<br>��������������<br>����������������<br>����������������<br>������������������<br>�����������������<br>��������������������� ����������������<br>�������������������<br>��������������<br>�����������������<br>������������������<br>����������<br>�������������������<br>������������<br>�����������������<br>���������������������<br>�����������������<br>������ ������<br>�������������������<br>���������������<br><!-- End of picture text -->

Figure 7: **Media statistics in HSS.** (a) Hierarchical breakdown of image and video assets across broad visual categories ( _People & social_ , _Work, craft & media_ , _Nature & outdoors_ , _Street & transport_ , and _Home & retail_ ) and fine-grained sub-scenes. (b) Distribution of visual scene types across the four core reasoning domains, showing diverse environment coverage per domain. (c) Histogram of video clip durations (log scale), spanning a few seconds to 28 minutes; summary statistics are in Table 3. 