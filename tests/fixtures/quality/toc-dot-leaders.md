s on Pure and Applied Mathematics_ , 13(1):1–14, 1960. doi: 10.1002/cpa.3160130102. 

- Yuhuai Wu, Albert Q. Jiang, Wenda Li, Markus N. Rabe, Charles Staats, Mateja Jamnik, and Christian Szegedy. Autoformalization with large language models. In _Advances in Neural Information Processing Systems (NeurIPS)_ , 2022. 

- Huajian Xin, Daya Guo, Zhihong Shao, Zhizhou Ren, Qihao Zhu, Bo Liu, Chong Ruan, Wenda Li, and Xiaodan Liang. DeepSeek-Prover: Advancing theorem proving in LLMs through large-scale synthetic data. _arXiv preprint arXiv:2405.14333_ , 2024. 

- Yilun Xu, Shengjia Zhao, Jiaming Song, Russell Stewart, and Stefano Ermon. A theory of usable information under computational constraints. In _International Conference on Learning Representations (ICLR)_ , 2020. 

- Kaiyu Yang, Aidan Swope, Alex Gu, Rahul Chalamala, Peiyang Song, Shixing Yu, Saad Godil, Ryan Prenger, and Anima Anandkumar. LeanDojo: Theorem proving with retrieval-augmented language models. In _Advances in Neural Information Processing Systems (NeurIPS)_ , 2023. 

- Huaiyuan Ying, Zijian Shi, Zhengying He, Bohan Gao, Chenyang Chen, Zhicheng Yu, Songrun Song, Qicheng Fan, Yinya Li, Lin Li, et al. Lean workbook: A large-scale Lean problem set formalized from natural language math problems. In _Advances in Neural Information Processing Systems (NeurIPS)_ , 2024. 

13 

# **Appendix** 

### **Contents** 

|**A Rel**|**ated Work**|**15**|
|---|---|---|
|**B Tra**|**ining the Difculty Model** _Vθ_|**15**|
|B.1|Data . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>15|
||B.1.1<br>Source corpus . . . . . . . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>15|
||B.1.2<br>Prompt . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>16|
|B.2|Model and Optimization . . . . . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>17|
||B.2.1<br>Training objective<br>. . . . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>17|
||B.2.2<br>Hyperparameters . . . . . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>18|
|B.3|Evaluation Protocols . . . . . . . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>18|
|**C Tra**|**ining a Conjecturing Model**|**18**|
|C.1|Conjecturing Model Training<br>. . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>18|
||C.1.1<br>Data construction<br>. . . . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>18|
||C.1.2<br>Compilation, relevance, and triviality<br>. . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>18|
||C.1.3<br>Optimization confguration<br>. . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>19|
||C.1.4<br>Theorem-proposal prompt . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>19|
|C.2|Conjecturing Model Evaluation . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>19|
||C.2.1<br>Balanced verifed cohort . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>19|
||C.2.2<br>Mathlib containment . . . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>20|
||C.2.3<br>Mathlib-containment rubric and judge prompt<br>. . . . . . . . . . .|. . . . . . . . . . .<br>20|
|C.3|Selected Generations . . . . . . . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>21|
|**D Infe**|**rence-Time Pruning Ablation**|**23**|
|D.1|Comparison across promotion policies<br>. . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>23|
|D.2|Judge prompts<br>. . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>24|
|**E Add**|**itional Figures**|**26**|
|E.1|Cross-area interestingness matrix . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>26|
|E.2|Iterative discovery graphs . . . . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>27|



14 

### **A Related Work** 

**Language models for formal theorem proving.** Language models can generate formal proof steps (Polu and Sutskever, 2020), with subsequent systems adding tree search and reinforcement learning (Lample et al., 2022; Gloeckle et al., 2024), library retrieval (Yang et al., 2023), synthetic data (Xin et al., 2024), subgoal decomposition, and test-time search (AlphaProof and AlphaGeometry teams, 2024; Ren et al., 2025; Lin et al., 2025; Wang et al., 2025). These methods primarily optimize completion of a fixed target under a particular prover and budget, whereas we aim to explore open-ended reasoning. 

**Autoformalization and formal libraries at scale.** LLM autoformalization has progressed from statementlevel translation (Wu et al., 2022) and paired datasets (Azerbayev et al., 2023; Ying et al., 2024) to large Lean developments from mathematical textbooks (Urban, 2026; Wang et al., 2026; Gloeckle et al., 2026). AutoformBot and Atlas furt