
import torch
import torch.nn as nn
import torch.nn.functional as F

from score_following_game.agents.networks_utils import weights_init, num_flat_features


class ScoreFollowingNetMSMDLCHSDeepDoLight(nn.Module):

    def __init__(self,  perf_shape, score_shape, use_cuda=False, hidden_dim=12, num_layers=1):
        super(ScoreFollowingNetMSMDLCHSDeepDoLight, self).__init__()

        self.use_cuda = use_cuda
        # spec part
        self.spec_conv1 = nn.Conv2d(perf_shape[0], out_channels=16, kernel_size=3, stride=1, padding=0)
        self.spec_conv2 = nn.Conv2d(self.spec_conv1.out_channels, out_channels=16, kernel_size=3, stride=1, padding=0)

        self.spec_conv3 = nn.Conv2d(self.spec_conv2.out_channels, out_channels=32, kernel_size=3, stride=2, padding=0)
        self.spec_conv4 = nn.Conv2d(self.spec_conv3.out_channels, out_channels=32, kernel_size=3, stride=1, padding=0)
        self.spec_do4 = nn.Dropout(p=0.2)

        self.spec_conv5 = nn.Conv2d(self.spec_conv4.out_channels, out_channels=64, kernel_size=3, stride=2, padding=0)

        self.spec_conv6 = nn.Conv2d(self.spec_conv5.out_channels, out_channels=96, kernel_size=3, stride=2, padding=0)
        self.spec_conv7 = nn.Conv2d(self.spec_conv6.out_channels, out_channels=96, kernel_size=1, stride=1, padding=0)
        self.spec_do7 = nn.Dropout(p=0.2)

        self.spec_fc = nn.Linear(2016, 512)

        # sheet part
        self.sheet_conv1 = nn.Conv2d(score_shape[0], out_channels=16, kernel_size=5, stride=(1, 2), padding=0)
        self.sheet_conv2 = nn.Conv2d(self.sheet_conv1.out_channels, out_channels=16, kernel_size=3, stride=1, padding=0)

        self.sheet_conv3 = nn.Conv2d(self.sheet_conv2.out_channels, out_channels=32, kernel_size=3, stride=2, padding=0)
        self.sheet_conv4 = nn.Conv2d(self.sheet_conv3.out_channels, out_channels=32, kernel_size=3, stride=1, padding=0)
        self.sheet_do4 = nn.Dropout(p=0.2)

        self.sheet_conv5 = nn.Conv2d(self.sheet_conv4.out_channels, out_channels=32, kernel_size=3, stride=2, padding=0)

        self.sheet_conv6 = nn.Conv2d(self.sheet_conv5.out_channels, out_channels=64, kernel_size=3, stride=2, padding=0)
        self.sheet_do6 = nn.Dropout(p=0.2)

        self.sheet_conv7 = nn.Conv2d(self.sheet_conv6.out_channels, out_channels=96, kernel_size=3, stride=2, padding=0)
        self.sheet_conv8 = nn.Conv2d(self.sheet_conv7.out_channels, out_channels=96, kernel_size=1, stride=1, padding=0)
        self.sheet_do8 = nn.Dropout(p=0.2)

        self.sheet_fc_prior = nn.Linear(10752, 1728)    # Added (sketch)
        self.sheet_fc = nn.Linear(1728, 512)

        # multi-modal part
        self.concat_fc = nn.Linear(512 + 512, 512)

        # recurrent layer
        self.num_layers = num_layers
        self.hidden_dim = hidden_dim
        self.gru_layer = nn.GRU(512, self.hidden_dim, self.num_layers, batch_first=True)   
        # fully connected layer
        self.final_fc = nn.Linear(self.hidden_dim*self.num_layers, 1)  # output is a single value representing distance on the score

        self.apply(weights_init)

    def forward(self, perf, score, hidden):

        #print("Forward performance shape: ", perf.shape)
        #print("Forward score shape: ", score.shape)
        spec_x = F.elu(self.spec_conv1(perf))
        spec_x = F.elu(self.spec_conv2(spec_x))
        spec_x = F.elu(self.spec_conv3(spec_x))
        spec_x = F.elu(self.spec_conv4(spec_x))
        spec_x = self.spec_do4(spec_x)
        spec_x = F.elu(self.spec_conv5(spec_x))
        spec_x = F.elu(self.spec_conv6(spec_x))
        spec_x = F.elu(self.spec_conv7(spec_x))
        spec_x = self.spec_do7(spec_x)

        spec_x = spec_x.view(-1, num_flat_features(spec_x))  # flatten
        spec_x = F.elu(self.spec_fc(spec_x))

        sheet_x = F.elu(self.sheet_conv1(score))
        sheet_x = F.elu(self.sheet_conv2(sheet_x))
        sheet_x = F.elu(self.sheet_conv3(sheet_x))
        sheet_x = F.elu(self.sheet_conv4(sheet_x))
        sheet_x = self.sheet_do4(sheet_x)
        sheet_x = F.elu(self.sheet_conv5(sheet_x))
        sheet_x = F.elu(self.sheet_conv6(sheet_x))
        sheet_x = self.sheet_do6(sheet_x)
        sheet_x = F.elu(self.sheet_conv7(sheet_x))
        sheet_x = F.elu(self.sheet_conv8(sheet_x))
        sheet_x = self.sheet_do8(sheet_x)

        sheet_x = sheet_x.view(-1, num_flat_features(sheet_x))  # flatten
        sheet_x = F.elu(self.sheet_fc_prior(sheet_x))   # Added (sketch)
        sheet_x = F.elu(self.sheet_fc(sheet_x))

        cat_x = torch.cat((spec_x, sheet_x), dim=1)

        cat_x = F.elu(self.concat_fc(cat_x))

        # Passing in the input and hidden state into the model and obtaining outputs
        cat_x = cat_x.unsqueeze(0)  # Sketch
        # hidden = self.init_hidden(cat_x.size(0)).cuda() if self.use_cuda else self.init_hidden(cat_x.size(0))
        out, hidden = self.gru_layer(cat_x, hidden)
        
        # Reshaping the outputs such that it can be fit into the fully connected layer
        # out = out.contiguous().view(-1, self.hidden_dim)
        #print("Forward out shape: ", score.shape)
        # out = self.final_fc(out)

        reshape_hidden = hidden.contiguous().view(1,-1)
        output = self.final_fc(reshape_hidden)
        
        return output, hidden

    def init_hidden(self, batch_size):
        # This method generates the first hidden state of zeros which we'll use in the forward pass
        # We'll send the tensor holding the hidden state to the device we specified earlier as well
        hidden = torch.randn(self.num_layers, batch_size, self.hidden_dim)

        if self.use_cuda:
            hidden = hidden.cuda()

        return hidden
