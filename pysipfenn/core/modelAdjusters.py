import os
from typing import Union, Literal, Tuple, List, Dict
from copy import deepcopy
import gc

import numpy as np
import torch
from torch.utils.data import DataLoader, TensorDataset
import plotly.express as px
import plotly.graph_objects as go
from pysipfenn.core.pysipfenn import Calculator

class LocalAdjuster:
    """
    Adjuster class taking a ``Calculator`` and operating on local data provided to model as a pair of descriptor data
    (provided in several ways) and target values (provided in several ways). It can then adjust the model with some predefined
    hyperparameters or run a fairly typical grid search, which can be interpreted manually or uploaded to the ClearML
    platform. Can use CPU, CUDA, or MPS (Mac M1) devices for training.

    Args:
        calculator: Instance of the ``Calculator`` class with the model to be adjusted, defined and loaded. It can
            contain the descriptor data already in it, so that it does not have to be provided separately.
        model: Name of the model to be adjusted in the ``Calculator``. E.g., ``SIPFENN_Krajewski2022_NN30``.
        targetData: Target data to be used for training the model. It can be provided as a path to a NumPy ``.npy``/
            ``.NPY`` or CSV ``.csv``/``.CSV`` file, or directly as a NumPy array. It has to be the same length as the
            descriptor data.
        descriptorData: Descriptor data to be used for training the model. It can be left unspecified (``None``) to
            use the data in the ``Calculator``, or provided as a path to a NumPy ``.npy``/``.NPY`` or CSV ``.csv``/
            ``.CSV`` file, or directly as a NumPy array. It has to be the same length as the target data. Default is
            ``None``.
        device: Device to be used for training the model. It Has to be one of the following: ``"cpu"``, ``"cuda"``, or
            ``"mps"``. Default is ``"cpu"``.
        descriptor: Name of the feature vector provided in the descriptorData. It can be optionally provided to
            check if the descriptor data is compatible.
        useClearML: Whether to use the ClearML platform for logging the training process. Default is ``False``.
        taskName: Name of the task to be used. Default is ``"LocalFineTuning"``.

    Attributes:
        calculator: Instance of the ``Calculator`` class being operated on.
        model: The original model to be adjusted.
        adjustedModel: A PyTorch model after the adjustment. Initially set to ``None``.
        descriptorData: NumPy array with descriptor data to use as input for the model.
        targetData: NumPy array with target data to use as output for the model.
    """

    def __init__(
            self,
            calculator: Calculator,
            model: str,
            targetData: Union[str, np.ndarray],
            descriptorData: Union[None, str, np.ndarray] = None,
            device: Literal["cpu", "cuda", "mps"] = "cpu",
            descriptor: Literal["Ward2017", "KS2022"] = None,
            useClearML: bool = False,
            taskName: str = "LocalFineTuning"
    ) -> None:
        self.adjustedModel = None
        self.useClearML = useClearML
        self.useClearMLMessageDisplayed = False
        self.taskName = taskName

        assert isinstance(calculator, Calculator), "The calculator must be an instance of the Calculator class."
        self.calculator = calculator

        self.device = torch.device(device)

        assert isinstance(model, str), "The model must be a string pointing to the model to be adjusted in the Calculator."
        assert model in self.calculator.models, "The model must be one of the models in the Calculator."
        assert model in self.calculator.loadedModels, "The model must be loaded in the Calculator."
        self.modelName = model
        self.model = self.calculator.loadedModels[model]
        self.model = self.model.to(device=self.device)

        if descriptorData is None:
            assert self.calculator.descriptorData is not None, "The descriptor data can be inferred from the data in the Calculator, but no data is present."
            self.descriptorData = self.calculator.descriptorData
        elif isinstance(descriptorData, np.ndarray):
            self.descriptorData = descriptorData
        elif isinstance(descriptorData, str):
            # Path to NPY file with data
            if (descriptorData.endswith(".npy") or descriptorData.endswith(".NPY")) and os.path.exists(descriptorData):
                self.descriptorData = np.load(descriptorData)
            # Path to CSV file with data
            elif (descriptorData.endswith(".csv") or descriptorData.endswith(".CSV")) and os.path.exists(descriptorData):
                self.descriptorData = np.loadtxt(descriptorData, delimiter=",")
            else:
                raise ValueError("If a string is provided as descriptor data parameter, it must be a path to a npy/NPY or csv/CSV file.")
        else:
            raise ValueError("The descriptor data must be either (a) None to use the data in the Calculator,"
                             "(b) a path to a npy/NPY file, or (c) a path to a csv/CSV file.")

        if isinstance(targetData, np.ndarray):
            self.targetData = targetData
        elif isinstance(targetData, str):
            # Path to NPY file with data
            if (targetData.endswith(".npy") or targetData.endswith(".NPY")) and os.path.exists(targetData):
                self.targetData = np.load(targetData)
            # Path to CSV file with data
            elif (targetData.endswith(".csv") or targetData.endswith(".CSV")) and os.path.exists(targetData):
                self.targetData = np.loadtxt(targetData, delimiter=",")
            else:
                raise ValueError("If a string is provided as target data parameter, it must be a path to a npy/NPY or csv/CSV file.")
        else:
            raise ValueError("The target data must be either a path to a npy/NPY file or a path to a csv/CSV file.")

        assert len(self.descriptorData) == len(self.targetData), "The descriptor and target data must have the same length."

        if descriptor is not None:
            if descriptor == "Ward2017":
                assert self.descriptorData.shape[1] == 271, "The descriptor must have 271 features for the Ward2017 descriptor."
            elif descriptor == "KS2022":
                assert self.descriptorData.shape[1] == 256, "The descriptor must have 256 features for the KS2022 descriptor."
            else:
                raise NotImplementedError("The descriptor must be either 'Ward2017' or 'KS2022'. Others will be added in the future.")

    def plotStarting(self) -> None:
        """
        Plot the starting model (before adjustment) on the target data. By default, it will plot in your browser.
        """
        self.model.eval()
        with torch.no_grad():
            dataIn = torch.from_numpy(np.array(self.descriptorData)).float().to(device=self.device)
            predictions = self.model(dataIn, None).detach().cpu().numpy().flatten()
        fig = px.scatter(
            x=self.targetData.flatten(),
            y=predictions,
            labels={
                "x": "Target Data", "y": "Predictions"},
            title="Starting (Unadjusted) Model Predictions")
        fig.show()

    def plotAdjusted(self) -> None:
        """
        Plot the adjusted model on the target data. By default, it will plot in your browser.
        """
        assert self.adjustedModel is not None, "The model must be adjusted before plotting. It is currently None."
        self.adjustedModel.eval()
        with torch.no_grad():
            dataIn = torch.from_numpy(np.array(self.descriptorData)).float().to(device=self.device)
            predictions = self.adjustedModel(dataIn, None).detach().cpu().numpy().flatten()
        fig = px.scatter(
            x=self.targetData.flatten(),
            y=predictions,
            labels={
                "x": "Target Data", "y": "Predictions"},
            title="Adjusted Model Predictions")
        fig.show()

    def adjust(
            self,
            validation: float = 0.2,
            learningRate: float = 1e-5,
            epochs: int = 50,
            batchSize: int = 32,
            optimizer: Literal["Adam", "AdamW", "Adamax", "RMSprop"] = "Adam",
            weightDecay: float = 1e-5,
            lossFunction: Literal["MSE", "MAE"] = "MAE",
            verbose: bool = True
    ) -> Tuple[torch.nn.Module, List[float], List[float]]:
        """
        Takes the original model, copies it, and adjusts the model on the provided data. The adjusted model is stored in
        the ``adjustedModel`` attribute of the class and can be then persisted to the original ``Calculator`` or used
        for plotting. The default hyperparameters are selected for fine-tuning the model rather than retraining it, as
        to slowly adjust it (1% of the typical learning rate) and not overfit it (50 epochs).

        Args:
            learningRate: The learning rate to be used for the adjustment. Default is ``1e-5`` that is 1% of a typical
                learning rate of ``Adam`` optimizer.
            epochs: The number of times to iterate over the data, i.e. how many times the model will see the data.
                Default is ``50``, which is on the higher side for fine-tuning. If the model does not retrain fast enough
                but already converged, consider lowering this number to reduce the time and possibly overfitting to the
                training data.
            batchSize: The number of points passed to the model at once. Default is ``32``, which is a typical batch size for
                smaller datasets. If the dataset is large, consider increasing this number to speed up the training.
            optimizer: Algorithm to be used for optimization. Default is ``Adam``, which is a good choice for most models
                and one of the most popular optimizers. Other options are
            lossFunction: Loss function to be used for optimization. Default is ``MAE`` (Mean Absolute Error / L1) that is
                more robust to outliers than ``MSE`` (Mean Squared Error).
            validation: Fraction of the data to be used for validation. Default is the common ``0.2`` (20% of the data).
                If set to ``0``, the model will be trained on the whole dataset without validation and you will not be able
                to check for overfitting or gauge the model's performance on unseen data.
            weightDecay: Weight decay to be used for optimization. Default is ``1e-5`` that should work well if data is
                plaintiful enough relative to the model complexity. If the model is overfitting, consider increasing this
                number to regularize the model more.
            verbose: Whether to print information, such as loss, during the training. Default is ``True``.

        Returns:
            A tuple with 3 elements: (1) the adjusted model, (2) training loss list of floats, and (3) validation loss
            list of floats. The adjusted model is also stored in the ``adjustedModel`` attribute of the class.
        """

        if verbose:
            print("Loading the data...")
        ddTensor = torch.from_numpy(self.descriptorData).float().to(device=self.device)
        tdTensor = torch.from_numpy(self.targetData).float().to(device=self.device)
        if validation > 0:
            split = int(len(ddTensor) * (1 - validation))
            ddTrain, ddVal = ddTensor[:split], ddTensor[split:]
            tdTrain, tdVal = tdTensor[:split], tdTensor[split:]
        else:
            ddTrain, ddVal = ddTensor, None
            tdTrain, tdVal = tdTensor, None

        datasetTrain = TensorDataset(ddTrain, tdTrain)
        dataloaderTrain = DataLoader(datasetTrain, batch_size=batchSize, shuffle=True)

        if verbose:
            print(f'LR: {learningRate} |  Optimizer: {optimizer}  |  Weight Decay: {weightDecay} |  Loss: {lossFunction}')
        # Training a logging platform. Completely optional and does not affect the training.
        if self.useClearML:
            if verbose and not self.useClearMLMessageDisplayed:
                print("Using ClearML for logging. Make sure to have (1) their Python package installed and (2) the API key"
                      " set up according to their documentation. Otherwise you will get an error.")
                self.useClearMLMessageDisplayed = True
            from clearml import Task
            task = Task.create(project_name=self.taskName,
                               task_name=f'LR:{learningRate} OPT:{optimizer} WD:{weightDecay} LS:{lossFunction}')
            task.set_parameters({'lr': learningRate,
                                 'epochs': epochs,
                                 'batch_size': batchSize,
                                 'weight_decay': weightDecay,
                                 'loss': lossFunction,
                                 'optimizer': optimizer,
                                 'model': self.modelName})
        if verbose:
            print("Copying and initializing the model...")
        model = deepcopy(self.model)
        model.train()
        if verbose:
            print("Setting up the training...")
        if optimizer == "Adam":
            optimizerClass = torch.optim.Adam
        elif optimizer == "AdamW":
            optimizerClass = torch.optim.AdamW
        elif optimizer == "Adamax":
            optimizerClass = torch.optim.Adamax
        elif optimizer == "RMSprop":
            optimizerClass = torch.optim.RMSprop
        else:
            raise NotImplementedError("The optimizer must be one of the following: 'Adam', 'AdamW', 'Adamax', 'RMSprop'.")
        optimizerInstance = optimizerClass(model.parameters(), lr=learningRate, weight_decay=weightDecay)

        if lossFunction == "MSE":
            loss = torch.nn.MSELoss()
        elif lossFunction == "MAE":
            loss = torch.nn.L1Loss()
        else:
            raise NotImplementedError("The loss function must be one of the following: 'MSE', 'MAE'.")

        transferLosses = [float(loss(model(ddTrain, None), tdTrain))]
        if validation > 0:
            validationLosses = [float(loss(model(ddVal, None), tdVal))]
            if verbose:
                print(
                    f'Train: {transferLosses[-1]:.4f} | Validation: {validationLosses[-1]:.4f} | Epoch: 0/{epochs}')
        else:
            validationLosses = []
            if verbose:
                print(f'Train: {transferLosses[-1]:.4f} | Epoch: 0/{epochs}')

        for epoch in range(epochs):
            model.train()
            for data, target in dataloaderTrain:
                optimizerInstance.zero_grad()
                output = model(data, None)
                lossValue = loss(output, target)
                lossValue.backward()
                optimizerInstance.step()
            transferLosses.append(float(loss(model(ddTrain, None), tdTrain)))

            if validation > 0:
                model.eval()
                validationLosses.append(float(loss(model(ddVal, None), tdVal)))
                model.train()
                if self.useClearML:
                    task.get_logger().report_scalar(
                        title='Loss',
                        series='Validation',
                        value=validationLosses[-1],
                        iteration=epoch+1)
                if verbose:
                    print(
                        f'Train: {transferLosses[-1]:.4f} | Validation: {validationLosses[-1]:.4f} | Epoch: {epoch + 1}/{epochs}')
            else:
                if verbose:
                    print(f'Train: {transferLosses[-1]:.4f} | Epoch: {epoch + 1}/{epochs}')

            if self.useClearML:
                task.get_logger().report_scalar(
                    title='Loss',
                    series='Training',
                    value=transferLosses[-1],
                    iteration=epoch+1)

        print("Training finished!")
        if self.useClearML:
            task.close()
        model.eval()
        self.adjustedModel = model
        del model
        del optimizerInstance
        del loss
        gc.collect()
        print("All done!")

        return self.adjustedModel, transferLosses, validationLosses

    def matrixHyperParameterSearch(
            self,
            validation: float = 0.2,
            epochs: int = 100,
            batchSize: int = 32,
            lossFunction: Literal["MSE", "MAE"] = "MAE",
            learningRates: Tuple[float] = (1e-6, 1e-5, 1e-4),
            optimizers: Tuple[Literal["Adam", "AdamW", "Adamax", "RMSprop"]] = ("Adam", "AdamW", "Adamax"),
            weightDecays: Tuple[float] = (1e-5, 1e-4, 1e-3),
            verbose: bool = True,
            plot: bool = True
    ) -> Tuple[torch.nn.Module, Dict[str, Union[float, str]]]:
        """
        Performs a grid search over the hyperparameters provided to find the best combination. By default, it will
        plot the training history with plotly in your browser, and (b) print the best hyperparameters found. If the
        ClearML platform was set to be used for logging (at the class initialization), the results will be uploaded
        there as well. If the default values are used, it will test 27 combinations of learning rates, optimizers, and
        weight decays. The method will then adjust the model to the best hyperparameters found, corresponding to the
        lowest validation loss if validation is used, or the lowest training loss if validation is not used
        (``validation=0``). Note that the validation is used by default.

        Args:
            validation: Same as in the ``adjust`` method. Default is ``0.2``.
            epochs: Same as in the ``adjust`` method. Default is ``100``.
            batchSize: Same as in the ``adjust`` method. Default is ``32``.
            lossFunction: Same as in the ``adjust`` method. Default is ``MAE``, i.e. Mean Absolute Error or L1 loss.
            learningRates: Tuple of floats with the learning rates to be tested. Default is ``(1e-6, 1e-5, 1e-4)``. See
                the ``adjust`` method for more information.
            optimizers: Tuple of strings with the optimizers to be tested. Default is ``("Adam", "AdamW", "Adamax")``. See
                the ``adjust`` method for more information.
            weightDecays: Tuple of floats with the weight decays to be tested. Default is ``(1e-5, 1e-4, 1e-3)``. See
                the ``adjust`` method for more information.
            verbose: Same as in the ``adjust`` method. Default is ``True``.
            plot: Whether to plot the training history after all the combinations are tested. Default is ``True``.
        """
        if verbose:
            print("Starting the hyperparameter search...")

        bestModel: torch.nn.Module = None
        bestTrainingLoss: float = np.inf
        bestValidationLoss: float = np.inf
        bestHyperparameters: Dict[str, Union[float, str, None]] = {
            "learningRate": None,
            "optimizer": None,
            "weightDecay": None,
            "epochs": None
        }

        trainLossHistory: List[List[float]] = []
        validationLossHistory: List[List[float]] = []
        labels: List[str] = []

        for learningRate in learningRates:
            for optimizer in optimizers:
                for weightDecay in weightDecays:
                    labels.append(f"LR: {learningRate} | OPT: {optimizer} | WD: {weightDecay}")
                    model, trainingLoss, validationLoss = self.adjust(
                        validation=validation,
                        learningRate=learningRate,
                        epochs=epochs,
                        batchSize=batchSize,
                        optimizer=optimizer,
                        weightDecay=weightDecay,
                        lossFunction=lossFunction,
                        verbose=True
                    )
                    trainLossHistory.append(trainingLoss)
                    validationLossHistory.append(validationLoss)
                    if validation > 0:
                        localBestValidationLoss, bestEpoch = min((val, idx) for idx, val in enumerate(validationLoss))
                        if localBestValidationLoss < bestValidationLoss:
                            print(f"New best model found with LR: {learningRate}, OPT: {optimizer}, WD: {weightDecay}, "
                                  f"Epoch: {bestEpoch + 1}/{epochs} | Train: {trainingLoss[bestEpoch]:.4f} | "
                                  f"Validation: {localBestValidationLoss:.4f}")
                            del bestModel
                            gc.collect()
                            bestModel = model
                            bestTrainingLoss = trainingLoss[bestEpoch]
                            bestValidationLoss = localBestValidationLoss
                            bestHyperparameters["learningRate"] = learningRate
                            bestHyperparameters["optimizer"] = optimizer
                            bestHyperparameters["weightDecay"] = weightDecay
                            bestHyperparameters["epochs"] = bestEpoch + 1
                        else:
                            print(f"Model with LR: {learningRate}, OPT: {optimizer}, WD: {weightDecay} did not improve.")
                    else:
                        localBestTrainingLoss, bestEpoch = min((val, idx) for idx, val in enumerate(trainingLoss))
                        if localBestTrainingLoss < bestTrainingLoss:
                            print(f"New best model found with LR: {learningRate}, OPT: {optimizer}, WD: {weightDecay}, "
                                  f"Epoch: {bestEpoch + 1}/{epochs} | Train: {localBestTrainingLoss:.4f}")
                            del bestModel
                            gc.collect()
                            bestModel = model
                            bestTrainingLoss = localBestTrainingLoss
                            bestHyperparameters["learningRate"] = learningRate
                            bestHyperparameters["optimizer"] = optimizer
                            bestHyperparameters["weightDecay"] = weightDecay
                            bestHyperparameters["epochs"] = bestEpoch + 1
                        else:
                            print(f"Model with LR: {learningRate}, OPT: {optimizer}, WD: {weightDecay} did not improve.")

        if verbose:
            print(f"\n\nBest model found with LR: {bestHyperparameters['learningRate']}, OPT: {bestHyperparameters['optimizer']}, "
                  f"WD: {bestHyperparameters['weightDecay']}, Epoch: {bestHyperparameters['epochs']}")
            if validation > 0:
                print(f"Train: {bestTrainingLoss:.4f} | Validation: {bestValidationLoss:.4f}")
            else:
                print(f"Train: {bestTrainingLoss:.4f}")
        assert bestModel is not None, "The best model was not found. Something went wrong during the hyperparameter search."
        self.adjustedModel = bestModel
        del bestModel
        gc.collect()

        if plot:
            fig1 = go.Figure()
            for idx, label in enumerate(labels):
                fig1.add_trace(
                    go.Scatter(
                        x=np.arange(epochs+1),
                        y=trainLossHistory[idx],
                        mode='lines+markers',
                        name=label)

            )
            fig1.update_layout(
                title="Training Loss History",
                xaxis_title="Epoch",
                yaxis_title="Loss",
                legend_title="Hyperparameters",
                showlegend=True,
                template="plotly_white"
            )
            fig1.show()
            if validation > 0:
                fig2 = go.Figure()
                for idx, label in enumerate(labels):
                    fig2.add_trace(
                        go.Scatter(
                            x=np.arange(epochs+1),
                            y=validationLossHistory[idx],
                            mode='lines+markers',
                            name=label)
                    )
                fig2.update_layout(
                    title="Validation Loss History",
                    xaxis_title="Epoch",
                    yaxis_title="Loss",
                    legend_title="Hyperparameters",
                    showlegend=True,
                    template="plotly_white"
                )
                fig2.show()

        return self.adjustedModel, bestHyperparameters



class OPTIMADEAdjuster(LocalAdjuster):
    """
    Adjuster class operating on data provided by the OPTIMADE API. Primarily geared towards tuning or retraining of the
    models based on other atomistic databases, or their subsets, accessed through OPTIMADE, to adjust the model to a
    different domain, which in the context of DFT datasets could mean adjusting the model to predict properties with DFT
    settings used by that database or focusing its attention to specific chemistry like, for instance, all compounds of
    Sn and all perovskites. It accepts OPTIMADE query as an input and then operates based on the ``LocalAdjuster`` class.
    """



