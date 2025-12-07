from django import forms
from .models import Product, AttributeDefinition

class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # 1. Find the store (from the existing product or the user's session)
        # Note: In Admin, getting the 'request' is tricky, so we rely on the instance.
        if self.instance and self.instance.pk and self.instance.store:
            store = self.instance.store
            definitions = AttributeDefinition.objects.filter(store=store, is_active=True)
            
            # 2. Create a field for each definition
            for attr in definitions:
                field_name = f"attr_{attr.key}" # Temporary name for the form
                initial_value = self.instance.attributes.get(attr.key, "")
                
                if attr.input_type == AttributeDefinition.InputType.SELECT:
                    # Create Dropdown
                    choices = [(opt, opt) for opt in attr.options]
                    choices.insert(0, ('', '---------'))
                    self.fields[field_name] = forms.ChoiceField(
                        label=attr.name, 
                        choices=choices, 
                        required=False, 
                        initial=initial_value
                    )
                else:
                    # Create Text Box
                    self.fields[field_name] = forms.CharField(
                        label=attr.name, 
                        required=False, 
                        initial=initial_value
                    )

    def save(self, commit=True):
        instance = super().save(commit=False)
        
        # 3. Pack the fields back into the JSON 'attributes' box
        if instance.store:
            definitions = AttributeDefinition.objects.filter(store=instance.store, is_active=True)
            for attr in definitions:
                field_name = f"attr_{attr.key}"
                if field_name in self.cleaned_data:
                    instance.attributes[attr.key] = self.cleaned_data[field_name]
        
        if commit:
            instance.save()
        return instance