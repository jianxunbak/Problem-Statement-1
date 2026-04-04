# -*- coding: utf-8 -*-
import Autodesk.Revit.DB as DB

class HideJoinFailuresPreprocessor:
    """
    Custom failure preprocessor to handle AutoJoin and other common BIM failures.
    NOTE: In Pythonnet 3.x (Revit 2026), do NOT inherit from DB.IFailuresPreprocessor.
    Instead, wrap an instance of this class using DB.IFailuresPreprocessor(instance).
    """
    
    def __init__(self, doc=None):
        """Initialize with optional document reference."""
        self.doc = doc
    
    def PreprocessFailures(self, failuresAccessor):
        """
        Process failures and suppress non-critical ones for better performance.
        Returns FailureProcessingResult to tell Revit how to proceed.
        """
        try:
            failure_messages = failuresAccessor.GetFailureMessages()
            
            for failure in failure_messages:
                failure_def_id = failure.GetFailureDefinitionId()
                severity = failure.GetSeverity()
                
                # Define failure types to suppress (these are the common AutoJoin issues)
                suppressed_failures = [
                    DB.BuiltInFailures.JoinElementsFailures.AttemptedJoinFailed,
                    DB.BuiltInFailures.JoinElementsFailures.CannotJoinElementsError,
                    DB.BuiltInFailures.JoinElementsFailures.CannotJoinElementsWarning,
                    DB.BuiltInFailures.GeneralFailures.DuplicateValue,
                    DB.BuiltInFailures.OverlapFailures.WallsOverlap,
                    DB.BuiltInFailures.OverlapFailures.FloorsOverlap
                ]
                
                # Suppress warnings and errors that are safe to ignore
                if failure_def_id in suppressed_failures:
                    if severity == DB.FailureSeverity.Warning:
                        failuresAccessor.DeleteWarning(failure)
                    elif severity == DB.FailureSeverity.Error:
                        # Try to resolve with default resolution first
                        if failure.HasResolutions():
                            try:
                                failuresAccessor.ResolveFailure(failure, failure.GetDefaultResolutionIndex())
                            except:
                                failuresAccessor.DeleteWarning(failure)
                        else:
                            failuresAccessor.DeleteWarning(failure)
                else:
                    # For other failures, try default resolution
                    if severity == DB.FailureSeverity.Warning:
                        failuresAccessor.DeleteWarning(failure)
                    elif failure.HasResolutions():
                        try:
                            failuresAccessor.ResolveFailure(failure, failure.GetDefaultResolutionIndex())
                        except:
                            pass
            
            return DB.FailureProcessingResult.Continue
            
        except Exception:
            # If anything goes wrong, continue rather than breaking the transaction
            return DB.FailureProcessingResult.Continue

class NuclearJoinGuard:
    """
    NEVER PERMIT JOINS: The most aggressive preprocessor for large-scale builds.
    Forces all join attempts to be ignored or deleted without calculation.
    """
    def __init__(self, doc=None):
        self.doc = doc

    def PreprocessFailures(self, failuresAccessor):
        try:
            failure_messages = failuresAccessor.GetFailureMessages()
            for failure in failure_messages:
                # 1. Broadly identify ANY join or overlap failure
                fid = str(failure.GetFailureDefinitionId())
                if "Join" in fid or "Overlap" in fid:
                    # Nuclear Option: Delete if warning, Resolve if Error
                    if failure.GetSeverity() == DB.FailureSeverity.Warning:
                        failuresAccessor.DeleteWarning(failure)
                    elif failure.HasResolutions():
                        failuresAccessor.ResolveFailure(failure, failure.GetDefaultResolutionIndex())
                
            return DB.FailureProcessingResult.Continue
        except:
            return DB.FailureProcessingResult.Continue
