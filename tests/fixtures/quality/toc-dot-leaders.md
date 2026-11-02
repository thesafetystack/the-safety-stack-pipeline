which suggests that the curriculum matters most in the first third of training and very little afterwards. We leave a finer schedule, and the question of whether the same holds for larger provers, to future work. 

11 

# **Appendix** 

### **Contents** 

|**A Ext**|**ended Background**|**12**|
|---|---|---|
|A.1|Proof search as a sequential decision problem . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>12|
|A.2|Curricula over problem difficulty . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>12|
|**B Est**|**imating Problem Difficulty**|**13**|
|B.1|Features of a problem statement . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>13|
||B.1.1<br>Syntactic features . . . . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>13|
||B.1.2<br>Features from earlier attempts . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>14|
|B.2|Regression model and its calibration . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>14|
|B.3|Agreement with human ratings . . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>15|
|**C Tra**|**ining Details**|**16**|
|C.1|Optimiser and schedule . . . . . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>16|
|C.2|Sampling temperature during search . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>16|
|C.3|Compute budget per run . . . . . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>17|
|**D Add**|**itional Experiments**|**18**|
|D.1|Curricula with a fixed difficulty order . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>18|
|D.2|Curricula that adapt to the prover . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>19|
|D.3|Effect of the number of search attempts . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>20|
|D.4|Transfer to held-out problem collections . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>21|
|**E Fai**|**lure Cases**|**22**|
|E.1|Problems the prover never solves . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>22|
|E.2|Proofs that pass the checker but are vacuous . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>23|
|**F Pro**|**mpts and Templates**|**24**|
|F.1|Statement formatting . . . . . . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>24|
|F.2|Tactic hints . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . .|. . . . . . . . . . .<br>25|

## **A Extended Background** 

### **A.1 Proof search as a sequential decision problem** 

A prover state is a list of open goals together with the local context of each. An action applies one tactic to the first goal and either fails, closes the goal, or replaces it with new subgoals. A proof is a sequence of actions that closes every goal, and its length is the number of tactics applied. 

Search proceeds by sampling tactics from the model, applying them, and keeping a frontier of the most promising states. We score a state by the model's log-probability of the tactics that led to it, normalised by their number, so that long and short partial proofs can be compared. 

### **A.2 Curricula over problem difficulty** 

A curriculum orders the training problems so that the prover meets easy problems before hard ones. The difficulty of a problem is not observed directly; we estimate it from features of its statement and from the outcomes of earlier attempts, as described in Appendix B. A curriculum is then a schedule that, at each stage of training, samples problems from a window of estimated difficulty that moves upwards as the prover improves. 

Two choices define a schedule: the width of the window and the rule that moves it. We compare fixed rules, which move the window by a set amount after each stage, with adaptive rules, which move it only once the prover solves a target share of the problems currently in the window. 
