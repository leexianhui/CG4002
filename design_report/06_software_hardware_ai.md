# Section 5: Software/Hardware AI

## 5.1 Ultra96 Synthesis and Simulation Setup

### 5.1.1 Partitioning Between PS and PL

The Ultra96-V2 is built around a Xilinx Zynq UltraScale+ ZU3EG MPSoC, which combines a Processing System (PS, quad-core ARM Cortex-A53) with Programmable Logic (PL, the FPGA fabric) on a single chip. Our design assigns work to each side according to what it is actually good at. The PS receives the `SENSOR_WINDOW` messages described in Section 4.5, handles weapon-model selection based on the player's current RFID-detected weapon, and returns `ACTION_RESULT` messages to the laptop once classification completes. The PL is reserved solely for the neural network inference itself, since that is the one stage of the pipeline where consistent, low, and measurable latency genuinely matters to how the duel feels.

### 5.1.2 Toolchain

Models are synthesized into FPGA logic using Vitis HLS, which takes a description of the trained, quantized network and produces synthesizable RTL. Vivado is then used to integrate the resulting IP blocks into the overall Ultra96 design, connected to the PS over an AXI-Stream interface, with a windowed and pre-processed feature vector going in and a predicted action label plus confidence score coming out. PYNQ is used during development to load and exercise the accelerator directly from Python, which lets us test classification behaviour before it is wired into the full C++/game-side pipeline.

### 5.1.3 Simulation Stages

Before any bitstream is deployed to the board, the design passes through three checks. The first is C simulation, where the HLS description of the model is run in software and its output is compared against the original floating point model on a held out batch of recorded IMU windows. The second is C/RTL co-simulation, which confirms that the synthesized hardware logic produces matching results to the C simulation and gives us early, pre-hardware estimates of inference latency in clock cycles. The third is hardware in the loop testing, where recorded gesture sequences captured during data collection are replayed through the deployed accelerator on the physical Ultra96. This lets us confirm the FPGA pipeline itself is correct before connecting it to a live, jittery sensor and network path, so that any problems found afterward can be attributed to sensing or communications rather than the inference core.

## 5.2 Neural Networks Explored

### 5.2.1 Two Model Families, Not One

The action set is split across two separate model families that reflect the way sensing itself is split in Section 3. The dominant wrist IMU feeds a weapon specific attack classifier, one trained model per weapon (Sword and Shield, Spear, Mace), selected at inference time using the player's RFID-detected weapon identity. The non-dominant wrist IMU feeds a single shared guard state classifier that determines whether that hand is actively bracing in a defensive posture or simply relaxed at rest. This mirrors the reasoning already laid out in Section 3.1.1: the two wrists are sensed and processed independently, so it follows that they are also classified independently rather than being folded into one combined model.

Separating attack recognition from guard detection, and separating attack recognition further by weapon, keeps each individual model small, keeps its training data requirement manageable, and keeps each one easy to validate and debug on its own rather than debugging one large model that has to reason about weapon identity, attack type, and guard state all at once.

### 5.2.2 Attack Action Classes Per Weapon

| Weapon | Recognized attack classes |
|---|---|
| Sword and Shield | Horizontal Swing, Vertical Swing, Diagonal Swing, Thrust, Charged Attack, Idle |
| Spear | Thrust, Charged Thrust, Swing, Idle |
| Mace | Vertical Swing, Diagonal Swing, Charged Attack, Idle |

Each weapon's attack classifier only needs to recognize the move set that weapon actually supports, per the feature list in Section 1. This keeps the class count, and therefore the model size, proportional to how much variety that weapon offers rather than uniformly large across all three. The guard state classifier, applied to the non-dominant wrist regardless of weapon, recognizes two classes: Guarding and Idle. Whether a successful guard actually blocks or merely reduces damage against a given weapon's attack is a rule applied afterward by the combat resolution engine in Section 4.7, not something the classifier itself needs to know.

### 5.2.3 Input Representation

Model input follows directly from the `SENSOR_WINDOW` schema already defined in Section 4.5: a short window of six channel samples per report, being the three axis acceleration and three axis angular velocity readings from the relevant wrist IMU, already calibrated, filtered, and expressed in the body relative frame established using the torso IMU as described in Section 3.2.2. Windows are assembled at the same roughly 10 Hz cadence at which `SENSOR_WINDOW` messages arrive, with each window covering a short span of samples wide enough to capture a full swing or thrust motion from start to finish.

### 5.2.4 Architecture

All four models (three attack classifiers, one guard classifier) use the same small one dimensional convolutional architecture, differing only in output class count. A 1D CNN was chosen over a recurrent architecture such as an LSTM because it is considerably simpler to quantize and synthesize through Vitis HLS, and because the motions being classified are short and self contained, typically completing within under a second, so there is little benefit to a model built around longer term sequence memory.

| Layer | Configuration |
|---|---|
| Input | Windowed samples, 6 channels |
| Conv1D | 16 filters, kernel size 5, ReLU |
| Conv1D | 32 filters, kernel size 5, ReLU |
| Global Average Pooling | |
| Dense | 32 units, ReLU |
| Dense | N units (per class count above), Softmax |

Each model is intentionally kept to a few thousand parameters, which keeps FPGA resource usage modest and keeps inference latency low, and gives us four small IP blocks to synthesize rather than one large one.

## 5.3 Training and Testing

### 5.3.1 Data Collection

All four team members perform each attack action, for each weapon, and each guard posture, in repeated sets of twenty to thirty or more repetitions. A small logging tool built alongside the FireBeetle firmware timestamps the start and end of each labelled repetition while streaming the corresponding raw IMU samples to a CSV file. Collecting from all four team members rather than a single person is deliberate, since it helps the resulting models generalize across differences in arm length, swing speed, and personal technique, which matters because the finished system needs to work reliably for any pair of players on demo day, not only for whoever trained it.

### 5.3.2 Preprocessing

Recorded sessions are cut into fixed length windows matching the model's input shape, normalized per channel using statistics computed from the training set, and lightly augmented with small time shifts and added noise. This augmentation step improves robustness to natural timing and sensor variation without requiring an unrealistically large number of physical repetitions to be collected.

### 5.3.3 Training

Training happens entirely offline, in TensorFlow and Keras, on a laptop or in Google Colab. The Ultra96 is not involved in this stage at all. Each dataset is split into training, validation, and test sets in a seventy, fifteen, fifteen ratio, stratified by class so that no gesture is over or under represented in any split.

### 5.3.4 Evaluation

For each of the four models, we report overall test set accuracy alongside a full per class confusion matrix, since some gesture pairs, such as diagonal and vertical swing, are naturally more visually and kinematically similar than others and we want that documented honestly rather than hidden behind a single aggregate number. Per class precision and recall are reported alongside the confusion matrix for the same reason. We additionally test each model using a leave one person out split, training on three team members' data and testing on the fourth, to get an honest read on how well the model generalizes to a player it has not seen before, which is closer to the actual demo condition than a random split across all four members combined.

## 5.4 Realizing the Models on the FPGA

### 5.4.1 Quantization

Each trained model is converted from full precision floating point to an eight bit integer representation, using a representative sample of training data to calibrate activation ranges during conversion. This quantization step is what makes the models small and fast enough to comfortably fit within the Ultra96's PL resources.

### 5.4.2 Conversion to Hardware

The quantized Keras models are converted into synthesizable Vitis HLS code using hls4ml, which is built specifically for small, latency sensitive models of this kind and removes the need to hand write the network in HLS directly. Each of the four models (three weapon specific attack classifiers, one shared guard classifier) is converted and synthesized independently.

### 5.4.3 Integration

Each synthesized classifier is instantiated in the Vivado design with an AXI-Stream input carrying the windowed, pre-processed feature vector, and an AXI-Stream or AXI-Lite output carrying the predicted class and confidence score. The PS selects which attack classifier instance to feed for a given player based on the most recently confirmed `WEAPON_CHANGED` event for that player, as reported through the RFID pipeline in Section 3. The guard classifier runs continuously on the non-dominant wrist stream regardless of weapon. Results from both classifiers for a given player, along with the weapon identity, are what forms the outgoing `ACTION_RESULT` message described in Section 4.5.

## 5.5 Evaluating Timing, Power, and Area

### 5.5.1 Timing

For each of the four models, end to end classification latency is measured on the physical Ultra96 hardware and compared directly against the same quantized model running on the ARM cores only, with no FPGA acceleration involved. This comparison is the primary justification for using the FPGA at all in this design, and the resulting numbers become our headline response time metric for the AI subsystem, reported per model since the four models differ slightly in size and class count.

### 5.5.2 Power

Power draw for each classifier's PL implementation is estimated using the Xilinx Power Estimator and the post implementation power report generated by Vivado, and compared against an estimate of the equivalent workload running on the ARM cores instead. This feeds into the overall wearable power budget covered in Section 3.5, and supports the position that FPGA acceleration is worthwhile for power efficiency per classification, not only for raw speed.

### 5.5.3 Area

FPGA resource usage, meaning look up tables, flip flops, block RAM, and DSP slices, is reported from the Vivado implementation report for each of the four synthesized classifiers, both individually and combined, as a percentage of what the Ultra96 makes available. This is used to confirm the combined design leaves enough headroom on the chip for the rest of the system to coexist comfortably.

### 5.5.4 Accuracy After Quantization

Finally, for each of the four models, test set accuracy is compared before and after quantization, so that any accuracy lost in exchange for the timing, power, and area benefits of running on the FPGA is shown explicitly rather than assumed to be negligible.
